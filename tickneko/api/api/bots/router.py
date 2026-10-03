"""机器人管理的 HTTP 入口（泛化版）：列表 / 添加 / 启用停用 / 删除。

    GET    <prefix>/bots          机器人列表（带归属昵称 + 在线状态）
    POST   <prefix>/bots          添加机器人（选 platform：onebot / kook）
    PATCH  <prefix>/bots/{id}     启用 / 停用
    DELETE <prefix>/bots/{id}     删除（OneBot 吊销令牌 / Kook 停并注销客户端）

这里认的是 :class:`~tickneko.api.api.bots.protocols.BotsService` 协议（装配层注入
:class:`tickneko.platforms.bridge.manager.BotManager`，平台差异封在实现里）：接口层不 import
任何平台包，「Kook 的启停要 start / stop 正向 WS 客户端」这类细节在这里不存在。OneBot 的
语义不再硬套给 Kook —— 接口统一成「增 / 启停 / 删」，底层按平台分派。

安全口径与 onebot 完全一致：都要登录；签发永远归当前登录用户；管理员不限、其余人只看
自己；按 id 找东西越界回 404（不泄露存在性）。
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request, status

from tickneko.bots import BotTokenConflict

from ...common.dependencies import trace_id_of
from ...common.errors import ApiError, ErrorCode
from ...common.models import ApiResponse, ErrorResponse
from ..onebot.dependencies import (
    CurrentUserDep,
    UserStoreDep,
    is_admin,
    may_touch,
    owner_filter_of,
)
from ..onebot.protocols import TokenLike
from .dependencies import BotsDep
from .protocols import BotsService
from .requests import AddBotRequest, SetBotEnabledRequest
from .responses import BotData, IssuedBotData

router = APIRouter(prefix="/bots", tags=["机器人管理"])


def _conflict(exc: BotTokenConflict) -> ApiError:
    """同一个令牌重复添加统一成 409（与工作流同名冲突同一口径）。"""
    return ApiError(
        ErrorCode.HTTP_ERROR,
        str(exc),
        status_code=status.HTTP_409_CONFLICT,
    )


async def _nicknames_of(users, owner_ids) -> dict[str, str]:  # type: ignore[no-untyped-def]
    """一次把一批归属 id 的昵称查齐（id -> 昵称），别一条条查（N+1）。"""
    records = await users.get_by_ids(owner_ids)
    return {owner_id: record.nickname for owner_id, record in records.items()}


async def _nickname_of(users, owner_id: str) -> str:  # type: ignore[no-untyped-def]
    """只装一条时用：拿归属 id 查昵称；查不到回空串。"""
    return (await _nicknames_of(users, [owner_id])).get(owner_id, "")


def _bot_of(record: TokenLike, nickname: str, *, online: bool, clients) -> BotData:  # type: ignore[no-untyped-def]
    """一条机器人记录 -> 机器人响应（补 platform + owner_id）。"""
    return BotData(
        id=record.id,
        platform=record.platform,
        owner_id=record.owner_id,
        account=record.account,
        enabled=record.enabled,
        remark=record.remark,
        created_at=record.created_at,
        nickname=nickname,
        online=online,
        clients=list(clients),
    )


async def _ensure_in_scope(user, service: BotsService, bot_id: str) -> TokenLike:  # type: ignore[no-untyped-def]
    """按 id 找机器人、确认在范围内；找不到 / 不是自己的走同一个 404。"""
    record = await service.get_by_id(bot_id)
    if record is None or not may_touch(user, record.owner_id):
        raise ApiError(ErrorCode.HTTP_ERROR, "没有这个机器人", status_code=status.HTTP_404_NOT_FOUND)
    return record


@router.get(
    "",
    response_model=ApiResponse[list[BotData]],
    summary="机器人列表",
    responses={status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse, "description": "没登录"}},
)
async def list_bots(
    request: Request,
    user: CurrentUserDep,
    service: BotsDep,
    users: UserStoreDep,
    owner_id: str | None = Query(default=None, max_length=64),
) -> ApiResponse[list[BotData]]:
    """机器人列表（**只有记录，明文拿不回来**）：非管理员只看得到自己那条。

    管理员默认看全部，``?owner_id=`` 可缩到某个归属（与工作流列表同一套口径）。
    """
    trace_id: str = trace_id_of(request)
    records = await service.list_records(owner_id=owner_filter_of(user, owner_id))
    names = await _nicknames_of(users, [record.owner_id for record in records])
    data: list[BotData] = []
    for record in records:
        nickname = names.get(record.owner_id, "")
        clients = service.online_clients(record)
        data.append(_bot_of(record, nickname, online=bool(clients), clients=clients))
    return ApiResponse[list[BotData]](data=data, trace_id=trace_id)


@router.post(
    "",
    response_model=ApiResponse[IssuedBotData],
    status_code=status.HTTP_200_OK,
    summary="添加机器人",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse, "description": "没登录"},
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": "同一个令牌 / Bot Token 已经添加过",
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse, "description": "kook 没填 Bot Token"},
    },
)
async def add_bot(
    payload: AddBotRequest,
    request: Request,
    user: CurrentUserDep,
    service: BotsDep,
    users: UserStoreDep,
) -> ApiResponse[IssuedBotData]:
    """给当前用户添加一个机器人；明文令牌只在这一次响应里出现。

    platform=onebot：随机签发令牌（不用填 token）；platform=kook：用自填的 Bot Token
    （可逆加密落库，正向 WS 客户端立刻拉起）。
    """
    trace_id: str = trace_id_of(request)
    try:
        issued = await service.issue(
            user.user.id,
            platform=payload.platform,
            account=payload.account,
            remark=payload.remark,
            token=payload.token or None,
        )
    except BotTokenConflict as exc:
        # 同一个令牌再添加一次：409 说清楚（别把裸 IntegrityError 一路滑到 500）
        raise _conflict(exc) from exc
    except ValueError as exc:
        # Kook 没配加密密钥这类「服务端没就绪」的问题：说清楚，别让 ValueError 一路滑到 500
        raise ApiError(
            ErrorCode.HTTP_ERROR, str(exc), status_code=status.HTTP_503_SERVICE_UNAVAILABLE
        ) from exc
    nickname = await _nickname_of(users, issued.record.owner_id)
    return ApiResponse[IssuedBotData](
        data=IssuedBotData(
            record=_bot_of(issued.record, nickname, online=False, clients=[]),
            token=issued.token,
        ),
        trace_id=trace_id,
    )


@router.patch(
    "/{bot_id}",
    response_model=ApiResponse[BotData],
    summary="启用 / 停用机器人",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse, "description": "没登录"},
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse, "description": "没有这个机器人"},
    },
)
async def set_bot_enabled(
    bot_id: str,
    payload: SetBotEnabledRequest,
    request: Request,
    user: CurrentUserDep,
    service: BotsDep,
    users: UserStoreDep,
) -> ApiResponse[BotData]:
    """启用 / 停用一个机器人；停用会断开 / 停掉它连着的客户端。"""
    trace_id: str = trace_id_of(request)
    record = await _ensure_in_scope(user, service, bot_id)
    changed = await service.set_enabled(bot_id, payload.enabled)
    if not changed:
        raise ApiError(ErrorCode.HTTP_ERROR, "没有这个机器人", status_code=status.HTTP_404_NOT_FOUND)
    nickname = await _nickname_of(users, record.owner_id)
    clients = service.online_clients(record)
    return ApiResponse[BotData](
        data=_bot_of(record, nickname, online=bool(clients), clients=clients),
        trace_id=trace_id,
    )


@router.delete(
    "/{bot_id}",
    response_model=ApiResponse[dict[str, bool]],
    summary="删除机器人",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse, "description": "没登录"},
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse, "description": "没有这个机器人"},
    },
)
async def delete_bot(
    bot_id: str,
    request: Request,
    user: CurrentUserDep,
    service: BotsDep,
) -> ApiResponse[dict[str, bool]]:
    """删除机器人：吊销令牌（OneBot）/ 停并注销客户端（Kook），并把正用着的连接断开。"""
    trace_id: str = trace_id_of(request)
    await _ensure_in_scope(user, service, bot_id)
    removed = await service.remove_by_id(bot_id)
    if not removed:
        raise ApiError(ErrorCode.HTTP_ERROR, "没有这个机器人", status_code=status.HTTP_404_NOT_FOUND)
    return ApiResponse[dict[str, bool]](data={"removed": True}, trace_id=trace_id)