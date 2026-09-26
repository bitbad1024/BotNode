"""写一个节点要用到的东西：执行函数的形状 + 它的运行时上下文 + 端口契约 + 注册规格。

**数据沿连线走，没有全局变量**：一个节点从自己的**输入端口**拿到上游送来的值，把产出放在
**输出端口**上，边就是这两者之间的管道（edge 的 ``source_port`` / ``target_port``）。

**这一份是对外契约**：别人写自己的节点时只从这里（以及 :mod:`.registry`）import，
不需要碰框架里别的文件::

    from nacho.workflow.nodes import (
        NodeExecutionContext, ConfigField, PortSpec, register_node, input_value,
    )

    @register_node(
        "dingtalk",
        # 输入端口：上游把消息接到 text 入口；required 表示「必须接线或手填」
        inputs=[PortSpec("text", "message", "消息内容", required=True)],
        outputs=[PortSpec("sent", "message", "是否发出")],
        # 同名字段 = 没接线时的手填兜底（连了线就用线上的值）
        fields=[ConfigField("text", "消息内容")],
    )
    async def exec_dingtalk(node, ctx: NodeExecutionContext) -> dict[str, object]:
        text = input_value(node, ctx, "text")
        ctx.logger.info("发钉钉消息", node_id=node.id, text=text)
        return {"sent": True}      # 键 = 输出端口名，下游连哪根线就拿到哪个值

函数签名就是 :data:`NodeExecutor`：收「节点 + 上下文」，返回**本节点的产出**（键必须是
已声明的输出端口名；引擎按边把它投递给下游对应入口）。

**校验什么由注册方自己说了算**：必填字段 / 默认值通过 :class:`ConfigField` 声明，端口与
「入口必填」通过 :class:`PortSpec` 声明，表格化覆盖不了的规则（枚举、条件必填）写一个
:data:`NodeConfigValidator` 挂上来，不用改校验器框架代码。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from nacho.core.logger import BaseLogger, get_logger
from nacho.core.scheduler import TaskManager

from ..models import ValidationIssue, WorkflowNode

#: 节点执行函数：(节点, 上下文) -> 本节点产出（键 = 已声明的输出端口名）
NodeExecutor = Callable[[WorkflowNode, "NodeExecutionContext"], Awaitable[dict[str, Any]]]

#: 节点在图中的拓扑角色：start=唯一入口 / end=终点 / normal=普通节点
NodeRole = Literal["start", "end", "normal"]

#: 节点配置校验器：收节点，返回校验问题列表（空列表 = 通过）
NodeConfigValidator = Callable[[WorkflowNode], list[ValidationIssue]]

#: 端口类型：trigger（控制流）决定「什么时候执行下一个节点」/ message（数据流）传内容
PortType = Literal["trigger", "message"]

#: 「字段没有声明默认值」的哨兵（None 也是合法默认值，不能拿 None 当缺省标记）
MISSING_DEFAULT: Any = object()

#: 上下文里「没有所属工作流」时的代号：离线跑 / 测试直接构造 ctx 的场合
NO_WORKFLOW_ID: str = "local"


@dataclass(frozen=True)
class ConfigField:
    """节点 ``config`` 里的一个字段声明：必填规则与默认值在注册时定死。

    * ``required=True``：缺失 / None / 空白字符串 → 校验直接报 ``MISSING_CONFIG``；
    * 给了 ``default``：缺失（键不存在或值为 None）时由
      :func:`nacho.workflow.validator.apply_config_defaults` 在保存版本时填默认值；
    * 两个都不给：纯可选字段，校验器不碰；
    * 给了 ``options``：这是**枚举**字段（画布渲染成下拉，顺序即显示顺序）。校验规则仍写在
      节点自己的 validator 里，这里只描述「有哪些可选值」。
    """

    name: str
    label: str = ""
    required: bool = False
    default: Any = MISSING_DEFAULT
    options: tuple[str, ...] | None = None


@dataclass(frozen=True)
class PortSpec:
    """节点一端的一个端口 —— **既是画布上的圆点，也是执行期的数据契约**。

    ``id`` 就是写进 edge 的 ``source_port`` / ``target_port`` 的那个值：

    * ``type="message"``（数据端口）：边**送值**。输出端口的值 = 执行函数返回值里同名的键；
      输入端口的值进 :attr:`NodeExecutionContext.inputs`，节点用 :func:`input_value` 取
      （同名的 :class:`ConfigField` 是「没接线时手填」的兜底）；
    * ``type="trigger"``（控制流端口）：边只表达「谁先谁后」，不送值。

    输入端口还多一个 ``required``：标了就必须**接上线或手填同名字段**，否则语义阶段报
    ``INPUT_NOT_CONNECTED``（输出端口忽略它）。

    :param id: 端口名（edge 两端引用的就是它；数据输出端口同时是产出值的键名）；
    :param type: 端口类型，连线两端必须同类；
    :param label: 显示名（缺省用 id）；
    :param required: 仅输入端口有效：必须接线（或同名字段手填了值）。
    """

    id: str
    type: PortType = "trigger"
    label: str = ""
    required: bool = False


#: 各节点通用的触发端口（出入口都叫「触发」）
TRIGGER_PORT = PortSpec("trigger", "trigger", "触发")


@dataclass(frozen=True)
class NodeSpec:
    """一种节点类型的完整注册规格：执行器 + 配置字段规则 + 拓扑约束。

    :param node_type: 类型名（节点 JSON 的 ``type``）；
    :param executor: 执行函数；声明了但执行器还没实现时为 None（跑到它才报暂无执行器）；
    :param fields: :class:`ConfigField` 清单，必填 / 默认值都从这里推导；
    :param validator: 自定义配置校验器（枚举、条件必填这类表格盖不住的规则）；
    :param role: 拓扑角色，start 全图唯一、end 至少一个可达；
    :param min_outgoing: 出边条数下限（分流类节点要 ≥2）；
    :param max_outgoing: 出边条数上限（end 为 0），None 不限；
    :param expression_field: 该字段内容要交图级表达式语法检查器过一遍；
    :param label: 显示名（画布面板项 / 节点标题），缺省用 ``node_type``；
    :param order: 画布面板顺序（小的在前，内置节点从 10 起）；
    :param inputs: 输入端口（画布左侧圆点；数据入口的值进 ``ctx.inputs``）；
    :param outputs: 输出端口（画布右侧圆点；执行函数返回值的键必须是这里的 id）。
    """

    node_type: str
    executor: NodeExecutor | None = None
    fields: tuple[ConfigField, ...] = ()
    validator: NodeConfigValidator | None = None
    role: NodeRole = "normal"
    min_outgoing: int = 0
    max_outgoing: int | None = None
    expression_field: str | None = None
    label: str = ""
    order: int = 100
    inputs: tuple[PortSpec, ...] = ()
    outputs: tuple[PortSpec, ...] = ()

def input_value(
    node: WorkflowNode, ctx: NodeExecutionContext, name: str, default: Any = ""
) -> Any:
    """取某个数据入口的值：**线上送来的优先，没接线才用 config 里同名字段的手填值**。

    这是「字段名 = 端口名」那条约定的唯一实现处，节点不用自己判断有没有接线::

        text = input_value(node, ctx, "message", default="")

    :param node: 当前节点（手填兜底从它的 ``config`` 取）；
    :param ctx: 运行时上下文（``inputs`` 里是引擎按边投递进来的值）；
    :param name: 入口名（数据端口的 id，通常与同名的 :class:`ConfigField` 一致）；
    :param default: 既没接线、config 里也没有这个键时返回什么。
    """
    if name in ctx.inputs:
        return ctx.inputs[name]
    return node.config.get(name, default)


class NodeExecutionContext:
    """节点运行时上下文：本节点的入口值 + 日志 + 调度器。

    ``inputs`` 是**属性**不是入参：引擎每跑一个节点前，按指向它的边把上游产出投递进来
    （键 = 目标端口名）。要预置入口值（测试 / 手动跑）直接写 ``ctx.inputs["x"] = ...``。

    :param logger: 业务日志实例（log 节点写这里）；
    :param scheduler: 调度器（时间触发的 start 节点把流程图登记到这里）；
    :param run: 触发整条流程的回调，cron 到点时调用；
    :param workflow_id: 这条图属于哪个工作流（时间触发登记任务时要它来保证任务名唯一，
        见 :func:`nacho.workflow.nodes.start.workflow_task_id`）；离线跑 / 测试直接构造
        ctx 时是 :data:`NO_WORKFLOW_ID`；
    :param register_triggers: 本次是不是「登记触发」那一趟（拨运行开关 / 启动载入 / 发布新版
        走的都是这一趟）：时间触发的 start 节点只有这时才去调度器加任务；整图执行（cron 到点
        跑整条流程）是 ``False`` —— 任务在调度器里排着，它自己会排下一次；
    :param multi_instance: 这条工作流的**实例策略**（工作流设置里的「单实例 / 多实例」，来自
        定义表，与图无关）：``False``（缺省，单实例）上一次还没跑完就跳过本次；``True``（多实例）
        到点就开新实例、允许叠加。只有登记那一趟用得上（交给调度器的 ``add``）。
    """

    def __init__(
        self,
        *,
        logger: BaseLogger | None = None,
        scheduler: TaskManager | None = None,
        run: Callable[[], Awaitable[None]] | None = None,
        workflow_id: str = NO_WORKFLOW_ID,
        register_triggers: bool = False,
        multi_instance: bool = False,
    ) -> None:
        self.inputs: dict[str, Any] = {}
        self.trigger_data: dict[str, Any] = {}  # 消息触发的入口数据（start 的 message 端口）
        self.log: list[str] = []  # 节点产出的文字日志（供测试 / 前端回显）
        self.workflow_id: str = workflow_id
        #: 本次是不是「登记触发」那一趟（见类文档）；整图执行时为 ``False``
        self.register_triggers: bool = register_triggers
        #: 实例策略：多实例时到点就开新实例（见类文档）
        self.multi_instance: bool = multi_instance
        self._logger: BaseLogger = logger if logger is not None else get_logger("workflow")
        self._scheduler: TaskManager | None = scheduler
        self._run: Callable[[], Awaitable[None]] | None = run

    @property
    def logger(self) -> BaseLogger:
        return self._logger

    @property
    def scheduler(self) -> TaskManager | None:
        return self._scheduler

    async def run_workflow(self) -> None:
        """cron 到点时触发整条流程的回调。"""
        if self._run is not None:
            await self._run()
