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
    limit / offset                   分页（按时间倒序：先排序，再翻页）
    processors                       只看某些出口（逗号分隔）；不写就是所有出口聚合

日志系统内部把出口的失败**吞掉并记账**（一个出口崩了不影响别的），所以这里先把明显写错的
参数挡住（级别名、时间格式）—— 否则它们会在出口里被吞掉，客户端只看到「一条都没有」。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, status

from nacho.core.logger import LogLevel, LogRecord, normalize_timestamp

from ...common.dependencies import trace_id_of
from ...common.errors import ApiError, ErrorCode, HttpStatus
from ...common.models import ApiResponse, ErrorResponse
from ..auth.dependencies import CurrentUserDep
from ..onebot.dependencies import ensure_can_touch, is_admin
from .dependencies import LoggerDep
from .responses import LogData

router = APIRouter(prefix="/logs", tags=["运行日志"])

#: 一次最多给多少条：再多请用 ``offset`` 翻页，别把整库日志一次拉出来
MAX_LIMIT: int = 500

#: 报错提示里能写哪些级别名
_LEVEL_NAMES: str = "/".join(level.name for level in LogLevel)


def _log_of(record: LogRecord) -> LogData:
    """把一条日志装成响应模型：字段原样搬，不在这里做格式化 / 过滤。"""
    return LogData(
        record_id=record.record_id,
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
    """``?processors=database,file`` 拆成出口名列表；不写就是所有出口。"""
    if not processors:
        return None
    names: list[str] = [name.strip() for name in processors.split(",") if name.strip()]
    return names or None


@router.get(
    "",
    response_model=ApiResponse[list[LogData]],
    summary="检索运行日志",
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "model": ErrorResponse,
            "description": "没登录 / 令牌无效",
        },
        status.HTTP_403_FORBIDDEN: {
            "model": ErrorResponse,
            "description": "要看别人的归属名下的日志",
        },
        HttpStatus.UNPROCESSABLE_ENTITY: {
            "model": ErrorResponse,
            "description": "级别名或时间参数写错",
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
    processors: Annotated[
        str | None, Query(description="只看某些出口（逗号分隔，如 database）")
    ] = None,
) -> ApiResponse[list[LogData]]:
    """检索日志：条件原样交给日志系统的 ``search``（落库那份就是一条 SQL）。

    非管理员不带 ``owner_id`` 时**默认只看自己的**；显式要别人的归属 -> 403（和 OneBot
    那组接口一个口径）。``processors`` 指定出口时，只查那些出口的结果。
    """
    trace_id: str = trace_id_of(request)
    if owner_id is None:
        if not is_admin(user):
            owner_id = user.user.id  # 不写就只看自己的，别把别人的漏出去
    else:
        ensure_can_touch(user, owner_id)

    records = await logger.search(
        query=query,
        level=_check_level(level),
        start=_check_moment(start, "start"),
        end=_check_moment(end, "end"),
        logger_name=logger_name,
        owner_id=owner_id,
        limit=limit,
        offset=offset,
        processors=_processor_names(processors),
    )
    data: list[LogData] = [_log_of(record) for record in records if _is_log(record)]
    return ApiResponse[list[LogData]](data=data, trace_id=trace_id)
