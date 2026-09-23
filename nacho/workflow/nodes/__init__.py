"""工作流节点：**一类节点一个文件**，文件里 import 契约 -> 写函数 -> 当场注册。

    nodes/
      base.py            契约：NodeExecutor 类型、NodeExecutionContext、render_variables
      registry.py        注册表：register_executor / @register_node / get_executor / load_node_modules
      start.py           内置节点：start（图起点；trigger=time 时按 cron 登记调度器）
      end.py             内置节点：end（图终点）
      log.py             内置节点：log（按级别写业务日志）
      test.py            内置节点：test（回显，画布联调用）
      http.py            内置节点：http（发一次 HTTP 请求，需要可选依赖 httpx）
      time_trigger.py    兼容：旧版 time-trigger 类型（历史版本快照），行为同 start 的时间触发

**写自己的节点**（不用改框架里的任何文件）：新建一个模块，写完让它在启动时被 import 到就行::

    # my_pkg/dingtalk.py
    from nacho.workflow.nodes import NodeExecutionContext, register_node, render_variables

    @register_node("dingtalk")
    async def exec_dingtalk(node, ctx: NodeExecutionContext) -> dict[str, object]:
        text = render_variables(str(node.config.get("text", "")), ctx.variables)
        ctx.logger.info("发钉钉消息", node_id=node.id, text=text)
        return {"sent": True}

    # 启动时（app.py 或自己的入口）
    from nacho.workflow import load_node_modules
    load_node_modules("my_pkg.dingtalk")

别忘了那几处白名单登记（**校验**用的，见 :mod:`nacho.workflow.validator`）：:data:`~nacho.workflow.models.NodeType`
与 ``NODE_TYPES`` / ``REQUIRED_CONFIG`` —— 类型不在白名单里，图在校验阶段就被拒，走不到执行。

完整指南（契约、上下文、命名、失败语义、白名单、可选依赖、测试写法）见
:file:`nacho/workflow/MODULES.md` 第 5 节。
"""

from __future__ import annotations

from .base import NodeExecutor, NodeExecutionContext, render_variables
from .end import exec_end
from .http import HTTP_METHODS, exec_http
from .log import exec_log
from .registry import (
    get_executor,
    load_node_modules,
    register_executor,
    register_node,
    registered_types,
)
from .start import exec_start
from .test import exec_test
from .time_trigger import exec_legacy_time_trigger

__all__ = [
    # 契约（写节点用这些）
    "NodeExecutor",
    "NodeExecutionContext",
    "render_variables",
    # 注册表
    "get_executor",
    "load_node_modules",
    "register_executor",
    "register_node",
    "registered_types",
    # 内置节点：import 上面那些模块即完成注册，函数本身也导出（复用 / 测试 / 换实现）
    "exec_start",
    "exec_end",
    "exec_log",
    "exec_test",
    "exec_http",
    "HTTP_METHODS",
    # 兼容旧版 time-trigger 类型（历史版本快照），新图请用 start + trigger=time
    "exec_legacy_time_trigger",
]
