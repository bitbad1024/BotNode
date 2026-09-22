"""工作流编排的领域模型：图（节点 / 边）、落库记录、校验结果。

这一层**不认识 FastAPI、也不认识数据库**：图就是前端画布设的那份 JSON
（``{"nodes": [...], "edges": [...]}``），记录在各层之间流转用的是冻结模型。

节点类型（``type`` 枚举）与入库前校验的阶段约定见 :mod:`nacho.workflow.validator`；
变量引用的统一写法是配置字符串里的 ``{{变量名}}``，作用域 = 沿边可达的前置节点。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- 图
#: 支持的节点类型；新增执行器类型时在这里登记，校验的「配置完整性」表在 validator 里
NodeType = Literal["start", "end", "gateway", "approval", "expression", "http", "condition", "task"]

#: 工作流状态：草稿（可继续改）/ 已发布（published_version 指的那份可被执行器取用）
WorkflowStatus = Literal["draft", "published"]


class WorkflowNode(BaseModel):
    """画布上的一个节点。

    :param id: 节点 ID，**一张图内全局唯一**（边的 source/target 指的就是它）；
    :param type: 节点类型，取值见 :data:`NodeType`；
    :param config: 节点配置（各类型要什么由校验器的配置完整性表把）；
    :param outputs: 本节点声明的输出变量名清单——下游用 ``{{名字}}`` 引用的凭据。

    不标 frozen：config 是 dict，pydantic 给 frozen 模型生成的 ``__hash__`` 会把全部字段
    值拿去 hash，dict 不可哈希会让「把节点放进集合」类操作直接炸；图模型按可解析数据对待即可。
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=64)
    type: str
    config: dict[str, Any] = Field(default_factory=dict)
    outputs: list[str] = Field(default_factory=list)


class WorkflowEdge(BaseModel):
    """一条有向边：``source`` 的输出流向 ``target``（也是变量作用域的传播方向）。"""

    model_config = ConfigDict(extra="ignore")

    source: str = Field(min_length=1, max_length=64)
    target: str = Field(min_length=1, max_length=64)


class WorkflowGraph(BaseModel):
    """工作流图：入库前校验与版本快照装的都是它。"""

    nodes: list[WorkflowNode] = Field(min_length=1)
    edges: list[WorkflowEdge] = Field(default_factory=list)


# --------------------------------------------------------------------------- 校验结果
#: 校验阶段名（顺序即入库前的流水线顺序，dry_run 可选）
STAGE_STRUCTURE: str = "structure"
STAGE_TOPOLOGY: str = "topology"
STAGE_SEMANTIC: str = "semantic"
STAGE_DRY_RUN: str = "dry_run"


class ValidationIssue(BaseModel):
    """一条校验错误：定位（nodeId）+ 机器码 + 给人看的话 + 修改建议。

    与前端约定的错误结构一致：节点级问题带节点 ID，整图级问题（如没有 start）留空串。
    """

    model_config = ConfigDict(frozen=True)

    node_id: str = ""
    code: str
    message: str
    suggestion: str = ""


class ValidationReport(BaseModel):
    """一次校验的完整结论；``valid=True`` 时 ``stage`` / ``errors`` 都是空的。

    失败在哪个阶段（``stage``）就说明后面的阶段没再跑——流水线**短路**：
    结构都不对，拓扑 / 语义无从谈起。
    """

    model_config = ConfigDict(frozen=True)

    valid: bool
    stage: str | None = None
    errors: list[ValidationIssue] = Field(default_factory=list)

    @classmethod
    def ok(cls) -> ValidationReport:
        """全过。"""
        return cls(valid=True)

    @classmethod
    def reject(cls, stage: str, errors: list[ValidationIssue]) -> ValidationReport:
        """卡在 ``stage``：带上该阶段收集到的错误（至少一条）。"""
        return cls(valid=False, stage=stage, errors=errors)


# --------------------------------------------------------------------------- 落库记录
class WorkflowDefinitionRecord(BaseModel):
    """工作流**定义**：一个工作流一行（元数据 + 当前 / 已发布版本指针）。"""

    model_config = ConfigDict(frozen=True)

    id: str
    #: 归属用户 id；多用户隔离就靠查询时强制带它（管理员可跨归属，见路由层）
    owner_id: str
    name: str
    status: WorkflowStatus = "draft"
    #: 最近一次保存的版本号；还没保存过图就是 0
    current_version: int = 0
    #: 已发布的版本号；从没发布过就是 0
    published_version: int = 0
    created_at: float
    updated_at: float


class WorkflowVersionRecord(BaseModel):
    """工作流**版本**：每次保存一张不可变的图快照（同内容不产生新版本，见 checksum）。"""

    model_config = ConfigDict(frozen=True)

    id: str
    workflow_id: str
    #: 冗余归属：按归属拉版本列表 / 鉴权时少一次 join，与定义上的 owner_id 一致
    owner_id: str
    version: int
    #: 图快照原文（规范 JSON 字符串，:func:`canonical_graph_json` 的产出）
    graph_json: str
    #: graph_json 的 sha256：内容没变就不新增版本
    checksum: str
    note: str = ""
    created_at: float

    def graph(self) -> WorkflowGraph:
        """把快照原文解析回 :class:`WorkflowGraph`（入库前已校验过，这里不该再失败）。"""
        return WorkflowGraph.model_validate(json.loads(self.graph_json))


# --------------------------------------------------------------------------- JSON / 摘要
def canonical_graph_json(graph: WorkflowGraph | dict[str, Any]) -> str:
    """把图序列化成**规范** JSON：键排序、无多余空白——checksum 与去重的基准。

    传入 dict 时先过一遍 :class:`WorkflowGraph`（丢未知字段、统一形态），
    保证「同一张图不同写法」（键顺序、空格）算出同一个摘要。
    """
    normalized = graph if isinstance(graph, WorkflowGraph) else WorkflowGraph.model_validate(graph)
    # 当前 pydantic 的 model_dump_json 不认 sort_keys，统一 dump 成 dict 再走标准库；
    # ensure_ascii=False：中文配置直接进摘要；sort_keys 让键顺序不影响结果
    return json.dumps(
        normalized.model_dump(mode="json"), sort_keys=True, ensure_ascii=False
    )


def graph_checksum(graph: WorkflowGraph | dict[str, Any]) -> str:
    """图内容的 sha256（64 位十六进制）；版本去重 / 变更比对用它。"""
    payload = canonical_graph_json(graph)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
