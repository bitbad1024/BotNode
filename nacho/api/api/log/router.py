"""运行日志的 HTTP 入口：检索。

    GET <prefix>/logs      检索日志（条件与日志系统的 ``search`` 一一对应）

要登录：``Authorization: Bearer <token>``，令牌是 ``POST /auth/login`` 给的那个。
**登录了还不算完，还得看范围**：带 ``admin`` 角色的人不限（所有人的日志都查得到），
其余人只看得见**自己名下**的（按 ``owner_id`` 过滤）—— 显式写别人的归属 → 403。

**查询条件原样透传**给日志系统的 :meth:`~nacho.core.logger.base.BaseLogger.search`，本层
不重新实现查询：它再扇出到各出口，落库那份就是一条 SQL（``WHERE`` / ``ORDER BY`` /
``LIMIT`` 全在数据库那边做完，不是捞回来再筛）：

    level / logger_name / owner_id    精确匹配（``owner_id=`` 给空串就是只看公共日志）
    query                            正文模糊匹配
    start / end                      时间**闭区间**，Unix 时间戳或 ISO 字符串都收
    limit / offset                   分页（按自增序号倒序：先排序，再翻页）
    source                           来源**类别**（``database`` / ``file`` / ``all``，**仅管理员**）
    processors                       只看某些**出口名**（逗号分隔，**仅管理员**）；与 source 二选一

响应给**一页**：``{ items, total }``——``items`` 是本页日志，``total`` 是条件命中的总条数
（翻页要它算总页数），前端据此做页码跳转。

**默认只查落库那份**（``database`` 出口）：查历史日志以库（SQL）为准 —— 控制台不留存，
文件那份是给人在本机翻的。库出口没开（``[logging.database] enabled = false``）时回 **503
并说清楚**，不然只会静默返回空，比报错难查得多。

**来源是类别还是名字**：界面上的「落库 / 本机文件 / 两者」说类别，用 ``?source=``（按出口
**类型**认，装配层怎么给文件出口起名都跟得上 —— 核心那份叫 ``file``、接口层 ``api.file``、
OneBot ``onebot.file``，「本机文件」= 这几路一起查）；要精确到某一路才用 ``?processors=``
给名字。两种来源**只有管理员能改**（普通用户想指定别的 -> 403），而且二选一，同写 -> 422。

日志系统内部把出口的失败**吞掉并记账**（一个出口崩了不影响别的），所以这里先把明显写错的
参数挡住（级别名、时间格式、来源类别、出口名）—— 否则它们会在出口里被吞掉，客户端只看到
「一条都没有」。出口名尤其要挡：日志系统对不认识的出口名是**直接忽略**的，
``processors=local``（实际那份叫 ``file``）就是「名字写错」与「真没有日志」表现一模一样。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Query, Request, status

from nacho.core.logger import (
    BaseLogProcessor,
    BaseLogger,
    DatabaseLogProcessor,
    LocalFileLogProcessor,
    LogLevel,
    LogRecord,
    normalize_timestamp,
)

from ...common.dependencies import trace_id_of
from ...common.errors import ApiError, ErrorCode, HttpStatus
from ...common.models import ApiResponse, ErrorResponse
from ..auth.dependencies import CurrentUserDep
from ..onebot.dependencies import ensure_can_touch, is_admin
from .dependencies import LoggerDep
from .responses import LogData, LogPage

router = APIRouter(prefix="/logs", tags=["运行日志"])

#: 一次最多给多少条：再多请用 ``offset`` 翻页，别把整库日志一次拉出来
MAX_LIMIT: int = 500

#: 不指定出口时查哪个：只查**落库那份**（SQL）。查历史以它为准——控制台不留存日志，
#: 文件那份是给人在本机翻的。名字取自处理机类的类属性，哪天改名这里跟着变。
DEFAULT_PROCESSOR: str = DatabaseLogProcessor.name

#: 报错提示里能写哪些级别名
_LEVEL_NAMES: str = "/".join(level.name for level in LogLevel)

#: 来源**类别** ->（出口类型，人话，没挂出口时怎么配）：界面上的「落库 / 本机文件」说的是
#: **类别**，不该逼前端记住出口叫什么名字 —— 名字由装配层起（核心那份叫 ``file``、接口层那份
#: 叫 ``api.file``、OneBot 那份叫 ``onebot.file``），前端写死一个，装配层一改名就对不上
#: （``processors=local`` 就是这么空手的）。这里按**类型**认：装配层怎么命名、挂了几路文件
#: 出口，都跟得上。三样放一起，加类别时不会漏掉其中一份。
SOURCE_KINDS: dict[str, tuple[type[BaseLogProcessor], str, str]] = {
    "database": (DatabaseLogProcessor, "落库", "配 [logging.database] enabled = true 才有得查"),
    "file": (LocalFileLogProcessor, "文件", "配 [logging.file] enabled = true 才有得查"),
}

#: ``source=all``：不限出口（库 + 文件 + 控制台都用上），对应界面上的「两者」。
SOURCE_ALL: str = "all"


def _log_of(record: LogRecord) -> LogData:
    """把一条日志装成响应模型：字段原样搬，不在这里做格式化 / 过滤。"""
    return LogData(
        record_id=record.record_id,
        seq=record.seq,
        timestamp=record.timestamp,
        level=record.level.name,
        logger_name=record.logger_name,
        owner_id=record.owner_id,
        message=record.message,
        extra=dict(record.extra),
        exc_text=record.exc_text,
    )


def _is_log(record: LogRecord) -> bool:
    """是不是一条真日志。

    控制台出口不留存日志，它的 ``search`` 会回一条「本出口不支持检索」的提示记录
    （``extra["search_supported"] = False``，给 REPL 里的人看的）—— 那不是日志，不进结果。
    """
    return record.extra.get("search_supported") is not False


def _check_level(level: str | None) -> LogLevel | None:
    """级别名先在这里校验：写错了要说出来，别让它落进出口被吞成「没有日志」。"""
    if level is None:
        return None
    try:
        return LogLevel.parse(level)
    except ValueError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"级别要 {_LEVEL_NAMES} 之一，收到 {level!r}",
            status_code=HttpStatus.UNPROCESSABLE_ENTITY,
        ) from exc


def _check_moment(value: str | None, field: str) -> str | None:
    """时间参数先在这里校验：收 Unix 时间戳或 ISO 字符串，认不出来就 422。

    只是**校验**（归一化交给出口那边，见 :func:`~nacho.core.logger.normalize_timestamp`），
    原值照传，免得把「秒 / 毫秒」这类语义在这一层说死。
    """
    if not value:
        return None
    try:
        normalize_timestamp(value)
    except ValueError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"{field} 要 Unix 时间戳或 ISO 时间字符串，收到 {value!r}",
            status_code=HttpStatus.UNPROCESSABLE_ENTITY,
        ) from exc
    return value


def _processor_names(processors: str | None) -> list[str] | None:
    """``?processors=database,file`` 拆成出口名列表。

    不写（或写空）返回 ``None``：由调用方决定默认查哪些出口（这里默认是落库那份）。
    """
    if not processors:
        return None
    names: list[str] = [name.strip() for name in processors.split(",") if name.strip()]
    return names or None


def _check_source(source: str | None) -> str | None:
    """来源类别先在这里校验：认不出来就 422，顺手把可用的几个列出来。"""
    if not source:
        return None
    if source == SOURCE_ALL or source in SOURCE_KINDS:
        return source
    raise ApiError(
        ErrorCode.VALIDATION_ERROR,
        f"来源要 {'/'.join(SOURCE_KINDS)}/{SOURCE_ALL} 之一，收到 {source!r}",
        status_code=HttpStatus.UNPROCESSABLE_ENTITY,
    )


def _channels_of_source(logger: BaseLogger, source: str) -> list[str] | None:
    """把来源类别翻成**实际挂着**的出口名（按类型认）；``None`` = 不限出口。

    类别下一个出口都没挂就回 503 并说清楚 —— 与「默认那份没落库」一个口径：静默给一个空
    列表，查的人只会当成「这段时间真没日志」。
    """
    if source == SOURCE_ALL:
        return None
    kind, label, hint = SOURCE_KINDS[source]
    names: list[str] = [p.name for p in logger.processor_registry if isinstance(p, kind)]
    if not names:
        raise ApiError(
            ErrorCode.HTTP_ERROR,
            f"没挂{label}出口，查不了：{hint}",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return names


def _ensure_channels(logger: BaseLogger, names: Sequence[str]) -> None:
    """``?processors=`` 里的名字必须**真的挂着**：写错了就说出来，别静默当成「没有日志」。

    日志系统对不认识的出口名是**直接忽略**（只挑认得的），于是「名字写错了」与「这段时间
    真没有日志」表现完全一样 —— ``processors=local``（实际那份叫 ``file``）就是这么查空的。
    """
    known: list[str] = [p.name for p in logger.processor_registry]
    unknown: list[str] = [name for name in names if name not in known]
    if unknown:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"不认识的日志出口：{'、'.join(unknown)}"
            f"（现在挂着：{'、'.join(known) if known else '一个都没有'}）",
            status_code=HttpStatus.UNPROCESSABLE_ENTITY,
        )


@router.get(
    "",
    response_model=ApiResponse[LogPage],
    summary="检索运行日志",
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "model": ErrorResponse,
            "description": "没登录 / 令牌无效",
        },
        status.HTTP_403_FORBIDDEN: {
            "model": ErrorResponse,
            "description": "要看别人的归属名下的日志，或非管理员指定日志来源",
        },
        HttpStatus.UNPROCESSABLE_ENTITY: {
            "model": ErrorResponse,
            "description": "级别名或时间参数写错",
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": "日志落库出口没开（默认只查落库那份）",
        },
    },
)
async def search_logs(
    request: Request,
    user: CurrentUserDep,  # 先鉴权：没登录就 401，不往外说有什么日志
    logger: LoggerDep,
    level: str | None = None,
    logger_name: str | None = None,
    query: str | None = None,
    owner_id: str | None = None,
    start: str | None = None,
    end: str | None = None,
    # 参数约束写在 Annotated 里，默认值不出现函数调用 —— 与依赖的
    # ``Annotated[..., Depends(...)]`` 一个写法，也避开「默认值里调函数」这条告警
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT, description="最多给多少条")] = 100,
    offset: Annotated[int, Query(ge=0, description="跳过前多少条（翻页用）")] = 0,
    source: Annotated[
        str | None,
        Query(description="按类别选来源：database / file / all（仅管理员）；与 processors 二选一"),
    ] = None,
    processors: Annotated[
        str | None, Query(description="只看某些出口（逗号分隔，仅管理员）；默认只查落库那份")
    ] = None,
) -> ApiResponse[LogPage]:
    """检索日志：条件原样交给日志系统的 ``search``（默认那份就是一条 SQL）。

    **默认只查落库那份**（``database`` 出口）：控制台不留存、文件那份是给人在本机翻的，
    查历史以库为准；库出口没开就回 503 并说清楚，别静默返回一个空列表。

    要换来源得**管理员**，两种写法（二选一）：

    * ``?source=`` 给**类别** —— ``database``（落库）、``file``（**所有**文件出口：核心那份
      ``file`` / 接口层 ``api.file`` / OneBot ``onebot.file`` 都算）、``all``（不限出口）。
      界面上「落库 / 本机文件 / 两者」走的就是它，按出口**类型**认，装配层怎么命名都跟得上；
    * ``?processors=`` 给**具体出口名**（逗号分隔），要精确到某一路时才用 —— 名字写错会 422
      并把现在挂着的出口列出来，不再静默当成「没有日志」。

    非管理员不带 ``owner_id`` 时**默认只看自己的**；显式要别人的归属 -> 403（和 OneBot
    那组接口一个口径）。
    """
    trace_id: str = trace_id_of(request)
    # 先校验参数：写错了就是写错了，与「出口开没开」这类配置问题分开报
    chosen_level = _check_level(level)
    chosen_start = _check_moment(start, "start")
    chosen_end = _check_moment(end, "end")
    chosen_source = _check_source(source)

    if owner_id is None:
        if not is_admin(user):
            owner_id = user.user.id  # 不写就只看自己的，别把别人的漏出去
    else:
        ensure_can_touch(user, owner_id)

    names = _processor_names(processors)
    if chosen_source is not None and names is not None:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "日志来源二选一：source 给类别（database/file/all），processors 给具体出口名",
            status_code=HttpStatus.UNPROCESSABLE_ENTITY,
        )

    if not is_admin(user):
        # 来源由管理员定：非管理员只能查默认那份，想指定别的来源 -> 403（自己写出来的参数
        # 越界就说清楚，别默默换成别的出口）。写成默认那份不报错——那跟不写是一回事。
        if chosen_source is not None or (
            names is not None and names != [DEFAULT_PROCESSOR]
        ):
            raise ApiError(
                ErrorCode.HTTP_ERROR,
                f"只有管理员能指定日志来源（source / processors）；默认查 {DEFAULT_PROCESSOR} 那份",
                status_code=status.HTTP_403_FORBIDDEN,
            )
        names = None

    if chosen_source is not None:
        names = _channels_of_source(logger, chosen_source)
    elif names is not None:
        _ensure_channels(logger, names)  # 名字得真的挂着，别静默查空
    else:  # 不指定就只查落库那份（SQL）
        names = _channels_of_source(logger, DEFAULT_PROCESSOR)

    # 一页与总数一起回来：同一套条件，不必再问一次「有多少条」
    result = await logger.search(
        query=query,
        level=chosen_level,
        start=chosen_start,
        end=chosen_end,
        logger_name=logger_name,
        owner_id=owner_id,
        limit=limit,
        offset=offset,
        processors=names,
    )
    data: list[LogData] = [_log_of(record) for record in result.records if _is_log(record)]
    return ApiResponse[LogPage](
        data=LogPage(items=data, total=result.total), trace_id=trace_id
    )
