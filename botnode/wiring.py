"""把进程日志核心派发到各业务模块的日志槽位（组合根的职责之一）。

依赖方向：本模块认识各业务包（同 :mod:`botnode.bootstrap`）；业务包不认识本模块。
``import`` 本模块**零副作用**（各 ``logging`` 接入点都是纯「槽位 + setter + 惰性取」
结构），核心真正生效要调一次 :func:`wire_loggers` —— 组合根（``botnode.bootstrap.run``）
与测试夹具（``tests/conftest.py``）各按自己的核心调。

未装配就调用某个业务便捷函数（``workflow_logger`` 等）会当场抛错（fail fast），
把「忘装配」变成显式错误，而不是静默落到一份参数不对的全局核心上。
"""
from __future__ import annotations

from botnode.api.logging import set_core as api_set_core
from botnode.core.cache.logging import set_core as cache_set_core
from botnode.core.logger import BaseLogger
from botnode.core.scheduler.logging import set_core as scheduler_set_core
from botnode.platforms.bridge.logging import set_core as bridge_set_core
from botnode.platforms.kook.logging import set_core as kook_set_core
from botnode.platforms.onebot.logging import set_core as onebot_set_core
from botnode.workflow.logging import set_core as workflow_set_core


def wire_loggers(core: BaseLogger | None) -> None:
    """把核心（``None`` = 清除）存进所有业务模块的日志槽位。"""
    cache_set_core(core)
    scheduler_set_core(core)
    bridge_set_core(core)
    workflow_set_core(core)
    api_set_core(core)
    onebot_set_core(core)
    kook_set_core(core)
