"""日志系统对外依赖的抽象协议。

日志系统不直接依赖具体数据库实现，只依赖这里的协议。
数据库层只需提供符合协议的对象，即可作为数据库日志处理机的后端，
从而保证「日志层」与「数据库层」解耦。

:class:`DatabaseAdapter` 约定的都是「基本能力」，没有可选 / 额外能力之分，
适配器必须全部实现：

* 查询：:meth:`DatabaseAdapter.fetch_all`；
* 增加：:meth:`DatabaseAdapter.execute` / :meth:`DatabaseAdapter.execute_many`；
* 清理历史日志：:meth:`DatabaseAdapter.delete_before`，
  调用方只需传入「删除该时刻之前」的时间参数。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from .models import TimestampLike


@runtime_checkable
class DatabaseAdapter(Protocol):
    """数据库适配器协议（由数据库层实现）。

    方法均为基本能力，适配器需要全部实现。
    """

    async def execute(self, sql: str, params: Sequence[object] | None = None) -> int:
        """增加：执行一条写语句（插入 / 更新 / 删除），返回受影响行数。"""
        ...

    async def execute_many(self, sql: str, rows: Sequence[Sequence[object]]) -> int:
        """增加：批量执行写语句（批量插入），返回受影响行数。"""
        ...

    async def fetch_all(
        self,
        sql: str,
        params: Sequence[object] | None = None,
    ) -> Sequence[Mapping[str, object]]:
        """查询：执行查询语句，返回全部结果行（每行为 mapping）。"""
        ...

    async def delete_before(self, before: TimestampLike) -> int:
        """清理历史日志：删除 ``before`` 时刻之前的记录，返回删除行数。

        约定：``before`` 是宽松输入（时间戳 / ISO 字符串 /
        :class:`datetime.datetime`），实现**必须**先用
        :func:`~nacho.core.logger.models.normalize_timestamp` 归一化成
        Unix 时间戳（``float`` 秒）再比较 / 拼 SQL；不要直接拼原始入参，
        也不要把规范形式改成字符串——默认表结构的 ``timestamp`` 列是
        ``DOUBLE PRECISION``，传字符串会类型不匹配、用不上索引，
        且各数据库方言的解析函数差异很大。

        :param before: 删除该时刻之前的日志；``None`` 属于非法输入，
            应抛 :class:`ValueError`。
        """
        ...
