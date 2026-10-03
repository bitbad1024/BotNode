"""数据库层：日志系统 :class:`~tickneko.core.logger.interfaces.LogStore` 协议的落地实现。

日志层只认协议、不关心底层是什么库；这一层负责把真正的连接收进来——但它**自己不建引擎、
不读配置**（连接参数归入口层管），只拿一个 :class:`~sqlalchemy.ext.asyncio.AsyncEngine`
说话：表用 SQLModel 声明、读写走 AsyncSession，不手写任何 SQL，所以 sqlite 与 mariadb
共用同一份定义、同一套代码。

* :class:`SqlLogStore`：日志落 ``logs`` 表（建表 / 批量写入 / 条件检索 / 清历史）。
"""

from __future__ import annotations

from .log_store import LogTable, SqlLogStore

__all__ = [
    "LogTable",
    "SqlLogStore",
]
