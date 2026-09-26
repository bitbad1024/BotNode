"""工作流节点：**一类节点一个文件**，文件里 import 契约 -> 写函数 -> 当场注册。

    nodes/
      base.py            契约：NodeExecutor / NodeSpec / ConfigField / PortSpec / 运行时上下文
      registry.py        注册表：register_node / declare_node_type / get_spec / load_node_modules
      start.py           内置节点：start（图起点；trigger=time 时按 cron 登记调度器）
      end.py             内置节点：end（图终点）
      log.py             内置节点：log（按级别写业务日志）
      test.py            内置节点：test（回显，画布联调用）
      constant.py        内置节点：constant（一个节点一个常量值，从 value 端口送下去）
      http.py            内置节点：http（发一次 HTTP 请求，需要可选依赖 httpx）
      delay.py           内置节点：delay（异步等待：触发进 / 触发出，秒数可接线覆盖手填）
      json.py            内置节点：json（解析 JSON 文本 + 点路径取值，取不到送空串不打断流程）

**数据沿连线走**：上游的输出端口 -> 下游的输入端口，值由执行引擎按边投递，没有全局变量。

**写自己的节点**（不用改校验器 / 框架里的任何文件）：新建一个模块，用装饰器把
「执行函数 + 端口 + 字段 + 自定义校验器」一次声明完，启动时 import 进来即可::

    # my_pkg/dingtalk.py
    from nacho.workflow.nodes import (
        ConfigField, NodeExecutionContext, PortSpec, input_value, register_node,
    )

    @register_node(
        "dingtalk",
        inputs=[PortSpec("text", "message", "消息内容", required=True)],  # 入口：接线或手填
        outputs=[PortSpec("sent", "message", "是否发出")],
        fields=[ConfigField("text", "消息内容")],  # 没接线时的手填兜底
    )
    async def exec_dingtalk(node, ctx: NodeExecutionContext) -> dict[str, object]:
        text = input_value(node, ctx, "text")
        ctx.logger.info("发钉钉消息", node_id=node.id, text=text)
        return {"sent": True}  # 键 = 输出端口名

    # 启动时（app.py 或自己的入口）
    from nacho.workflow import load_node_modules
    load_node_modules("my_pkg.dingtalk")

类型**注册即合法**：校验器从同一张注册表推导「认不认识这个类型、要查哪些字段」，
不再维护任何框架侧白名单。

完整指南（契约、上下文、命名、失败语义、孤儿节点、可选依赖、测试写法）见
:file:`nacho/workflow/MODULES.md` 第 5 节。
"""

from __future__ import annotations

from .base import (
    MISSING_DEFAULT,
    TRIGGER_PORT,
    ConfigField,
    NodeConfigValidator,
    NodeExecutionContext,
    NodeExecutor,
    NodeRole,
    NodeSpec,
    PortSpec,
    PortType,
    input_value,
)
from .constant import exec_constant
from .delay import exec_delay
from .end import exec_end
from .http import HTTP_METHODS, exec_http
from .json import exec_json
from .log import LOG_LEVELS, exec_log
from .registry import (
    declare_node_type,
    get_executor,
    get_spec,
    load_node_modules,
    register_executor,
    register_node,
    registered_types,
)
from .start import (
    START_TRIGGERS,
    exec_start,
    validate_start_node,
    workflow_task_id,
)
from .test import exec_test

__all__ = [
    # 契约（写节点用这些）
    "NodeExecutor",
    "NodeExecutionContext",
    "NodeSpec",
    "NodeRole",
    "NodeConfigValidator",
    "ConfigField",
    "PortSpec",
    "PortType",
    "TRIGGER_PORT",
    "MISSING_DEFAULT",
    "input_value",
    # 注册表
    "register_executor",
    "register_node",
    "declare_node_type",
    "get_executor",
    "get_spec",
    "registered_types",
    "load_node_modules",
    # 内置节点：import 上面那些模块即完成注册，函数本身也导出（复用 / 测试 / 换实现）
    "exec_start",
    "validate_start_node",
    "START_TRIGGERS",
    "workflow_task_id",
    "exec_end",
    "exec_log",
    "LOG_LEVELS",
    "exec_constant",
    "exec_test",
    "exec_http",
    "HTTP_METHODS",
    "exec_delay",
    "exec_json",
]
