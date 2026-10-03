"""测试夹具：给各业务模块的日志槽位做自动装配。

业务模块的便捷函数（``workflow_logger`` 等）不再自己 ``default_core()``，而是取装配槽位；
这里每个测试前把槽位指到进程默认核心（没有就懒建一份，与改造前的行为对齐），
测试后清空，避免互相污染。专门测日志核心本身的测试（``test_log_*``）自管核心，
与本夹具互不干扰（它们大多走 ``manager.reset()``）。
"""
from __future__ import annotations

import pytest

from tickneko.core.logger import current_default_core, default_core
from tickneko.wiring import wire_loggers


@pytest.fixture(autouse=True)
def _wire_business_loggers() -> None:
    wire_loggers(current_default_core() or default_core())
    yield
    wire_loggers(None)
