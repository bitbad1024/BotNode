"""控制台日志处理机。

用于「最小化启动」阶段：日志系统实例化后立刻有一路可见的输出，
不依赖文件、数据库等外部后端，因此不必等业务侧把后端准备好。

两个刻意的取舍：

* ``buffer_size=1`` 且 ``flush_interval=0``：控制台是给人看的，攒批只会让
  问题现场难以对齐，所以逐条直写、不缓冲；
* :meth:`ConsoleLogProcessor.search` 不返回历史日志，而是返回**一条提示记录**：
  控制台输出不留存，回读没有意义，提示需要检索请挂本地文件或数据库处理机。
  与其静默返回空列表，让人误以为「真的没有日志」，不如显式说明本出口不支持检索。

输出是**同步直写**（不走 :func:`asyncio.to_thread`）：控制台通常对应终端或
管道，单条写入极快，为每条日志付一次线程调度并不划算。若把控制台当作高频
日志的唯一出口，应改用本地文件处理机。
"""
from __future__ import annotations

import sys
from typing import TextIO, override

from ..models import LogLevel, LogRecord, LogSearchResult, TimestampLike
from .base import BaseLogProcessor, ProcessorStats

#: 各日志级别的 ANSI 颜色，仅在 ``color=True`` 时使用
_LEVEL_COLORS: dict[LogLevel, str] = {
    LogLevel.DEBUG: "\033[36m",
    LogLevel.INFO: "\033[32m",
    LogLevel.WARNING: "\033[33m",
    LogLevel.ERROR: "\033[31m",
    LogLevel.CRITICAL: "\033[1;31m",
}
_RESET: str = "\033[0m"


class ConsoleProcessorStats(ProcessorStats):
    """控制台处理机的运行统计：在基类字段之外追加颜色开关标记。

    继承 :class:`~botnode.core.logger.processors.base.ProcessorStats` 而不是新开一个
    字典类型，覆写后的返回类型就是基类的**子类型**，既满足 LSP（里氏替换）也不会
    丢失基类字段的类型信息。
    """

    #: 是否输出 ANSI 颜色
    color: bool


class ConsoleLogProcessor(BaseLogProcessor):
    """把日志逐条写到控制台（默认 ``sys.stdout``）。"""

    name: str = "console"
    #: 控制台是全局观感出口：子模块自层挂了文件出口后，它仍继续输出
    inherit_on_override: bool = True

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        color: bool = True,
        name: str | None = None,
    ) -> None:
        """
        :param stream: 输出流；默认在每次写出时动态取 ``sys.stdout``，
            因此在重定向 ``sys.stdout`` 的测试 / 宿主环境里也能正确输出。
        :param color: 是否用 ANSI 颜色区分级别，默认开启。

        控制台不做任何过滤：模块路由与级别过滤都由分发器负责。想让控制台只收
        更高级别的日志，请在挂载时给它一个出口级 ``level`` 门槛，例如
        ``core.mount(ConsoleLogProcessor(), level="WARNING")``。
        """
        # 逐条直写：控制台输出不攒批，出问题时也不会压在缓冲区里
        super().__init__(name=name, buffer_size=1, flush_interval=0)
        self._stream: TextIO | None = stream
        self._color: bool = color

    # ------------------------------------------------------------------ 写入
    @override
    async def write(self, records: list[LogRecord]) -> None:
        if not records:
            return
        self._write_sync("".join(self._format(record) for record in records))

    def _write_sync(self, text: str) -> None:
        stream: TextIO = self._stream if self._stream is not None else sys.stdout
        stream.write(text)
        stream.flush()

    def _format(self, record: LogRecord) -> str:
        # 有所有者的（api / ws 这类明确归属的操作）在实例名后带上，如 botnode.api.access(u-admin)；
        # 公共日志（空串）不加，免得给绝大多数行都挂一对空括号
        owner = f"({record.owner_id})" if record.owner_id else ""
        text = (
            f"[{record.datetime_text}] {record.level.name:<8} "
            f"{record.logger_name}{owner} {record.message}"
        )
        if record.extra:
            text += f" {record.extra}"
        text += "\n"
        if record.exc_text:
            text += (
                record.exc_text
                if record.exc_text.endswith("\n")
                else f"{record.exc_text}\n"
            )
        if not self._color:
            return text
        return f"{_LEVEL_COLORS[record.level]}{text}{_RESET}"

    # ------------------------------------------------------------------ 检索
    @override
    async def search(
        self,
        *,
        query: str | None = None,
        level: LogLevel | str | None = None,
        start: TimestampLike = None,
        end: TimestampLike = None,
        logger_name: str | None = None,
        owner_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> LogSearchResult:
        """控制台不留存日志：返回一条提示记录，说明本出口不支持检索。

        不返回空列表，是为了避免调用方把「控制台查不到」误当成「真的没有日志」。
        提示记录带有 ``extra["search_supported"] = False`` 标记，便于上游识别并
        从结果里剔除；``limit <= 0`` 时仍返回空列表。总数恒为 0 —— 这里确实没东西可数。
        """
        if limit <= 0:
            return LogSearchResult()
        return LogSearchResult(
            records=[
                LogRecord(
                    message=(
                        "控制台处理机不支持日志检索：控制台输出不留存，"
                        "需要回读请改用本地文件或数据库处理机"
                    ),
                    level=LogLevel.WARNING,
                    logger_name=self.name,
                    extra={"processor": self.name, "search_supported": False},
                )
            ]
        )

    # ------------------------------------------------------------------ 状态
    @property
    @override
    def stats(self) -> ConsoleProcessorStats:
        """基类统计 + ``color``：返回类型是基类 ``ProcessorStats`` 的子类型。

        ``super().stats`` 每次访问都新建一份字典，因此可以放心在其基础上追加字段。
        """
        return ConsoleProcessorStats(**super().stats, color=self._color)
