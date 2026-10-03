"""日志检索接口的响应体。

一条日志就是 :class:`LogData` 的**结构化字段**原样给出（级别、模块、所有者、附加字段……），
不在这里拼成人看的字符串：怎么显示归前端，时间也只给 Unix 时间戳。
检索接口一次给**一页**（:class:`LogPage`）：本页条目 + 命中总数，前端据此算总页数、做页码跳转。
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field


class LogData(BaseModel):
    """一条运行日志。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    #: 记录编号（同一批日志去重、回头对某一条时用）
    record_id: str = Field(description="记录编号")
    #: 自增序号（落库那份的插入顺序，本页就是按它倒序的）；查的是不留存序号的出口（如文件）时是 0
    seq: int = Field(default=0, description="自增序号（插入顺序；0 = 该出口不留存序号）")
    #: Unix 时间戳（秒）；前端自己按本地时区格式化
    timestamp: float = Field(description="Unix 时间戳（秒）")
    #: 级别名：DEBUG / INFO / WARNING / ERROR / CRITICAL
    level: str = Field(description="级别名")
    #: 模块名（写日志的实例名，如 ``tickneko.api`` / ``tickneko.onebot``）
    logger_name: str = Field(default="", description="模块名")
    #: 所有者：谁的操作；空串 = 公共（框架自身、没有归属的请求）
    owner_id: str = Field(default="", description="所有者；空串 = 公共")
    #: 日志正文
    message: str = Field(description="日志正文")
    #: 附加字段（结构化那部分，如 ``trace_id`` / ``robot_id``）
    extra: dict[str, object] = Field(default_factory=dict, description="附加字段")
    #: 异常栈文本；没有异常就是 ``null``
    exc_text: str | None = Field(default=None, description="异常栈文本")


class LogPage(BaseModel):
    """日志检索的**一页**：本页条目 + 命中总数（前端据此算总页数、做页码跳转）。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    #: 本页的日志（按自增序号倒序，即插入顺序的倒序）
    items: list[LogData] = Field(description="本页日志条目")
    #: 命中条件的**总条数**（不受本页 ``limit`` / ``offset`` 限制）
    total: int = Field(description="命中总条数")
