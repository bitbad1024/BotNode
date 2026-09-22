"""运行日志入口：``GET <prefix>/logs`` 检索日志。

这一包只做「对外怎么说」：日志从哪来、范围怎么判在 :mod:`.dependencies`，响应长什么样在
:mod:`.responses`，查询本身**不在这里实现** —— 条件原样递给日志系统的 ``search``，
再往下是各出口自己的事（落库那份就是一条 SQL，见 :class:`~nacho.db.SqlLogStore`）。
"""

from __future__ import annotations

from .responses import LogData
from .router import router as log_router

__all__ = [
    "LogData",
    "log_router",
]
