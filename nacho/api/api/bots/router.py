"""机器人管理的 HTTP 入口（泛化版）：列表 / 添加 / 启用停用 / 删除。

    GET    <prefix>/bots          机器人列表（带归属昵称 + 在线状态）
    POST   <prefix>/bots          添加机器人（选 platform：onebot / kook）
    PATCH  <prefix>/bots/{id}     启用 / 停用
    DELETE <prefix>/bots/{id}     删除（吊销令牌 / 撤销凭证）

P5 过渡：这是 `/api/onebot/*` 的**泛化壳** —— 路径 / 名字 / ``platform`` 字段先立起来，
底层仍复用 OneBot 的令牌注册表（``OneBotLike``），OneBot 语义不变；Kook 的「添加」
在 P6 才真正可用（现在 platform=kook 回 501）。多实例 / 换到新表在 P5-4 统一切。

安全口径与 onebot 完全一致：都要登录；签发永远归当前登录用户；管理员不限、其余人只看
自己；按 id 找东西越界回 404（不泄露存在性）。
"""
from __future__ import annotations

from fastapi import APIRouter, Request, status

from ...common.dependencies import trace_id_of
from ...common.errors import ApiError, ErrorCode
from ...common.models import ApiResponse, ErrorResponse
from ..onebot.dependencies import (
    CurrentUserDep,
    OneBotDep,
    UserStoreDep,
    ensure_can_touch,
    is_admin,
    may_touch,
)
from ..onebot.protocols import OneBotLike, TokenLike
from .requests import AddBotRequest, SetBotEnabledRequest
from .responses import BotData, IssuedBotData

router = APIRouter(prefix="/bots", tags=["机器人管理"])


async def _nicknames_of(users, owner_ids) -> dict[str, str]:  # type: ignore[no-untyped-def]
    """一次把一批归属 id 的昵称查齐（id -> 昵称），别一条条查（N+1）。"""
    records = await users.get_by_ids(owner_ids)
    return {owner_id: record.nickname for owner_id, record in records.items()}


async def _nickname_of(users, owner_id: str) -> str:  # type: ignore[no-untyped-def]
    """只装一条时用：拿归属 id 查昵称；查不到回空串。"""
    return (await _nicknames_of(users, [owner_id])).get(owner_id, "")


def _bot_of(record: TokenLike, nickname: str, *, online: bool, clients) -> BotData:  # type: ignore[no-untyped-def]
    """一条令牌记录 -> 机器人响应（补 platform=onebot）。"""
    return BotData(
        id=record.id,
        platform="onebot",  # 过渡期：底层只有 onebot，platform 恒 onebot
        account=record.account,
        enabled=record.enabled,
        remark=record.remark,
        created_at=record.created_at,
        nickname=nickname,
        online=online,
        clients=list(clients),
    )


async def _ensure_in_scope(user, server: OneBotLike, bot_id: str) -> TokenLike:  # type: ignore[no-untyped-def]
    """按 id 找机器人、确认在范围内；找不到 / 不是自己的走同一个 404。"""
    registry = server.tokens
    if registry is None:
        raise ApiError(
            ErrorCode.HTTP_ERROR, "没配令牌注册表", status_code=status.HTTP_404_NOT_FOUND
        )
    record = await registry.get_by_id(bot_id)
    if record is None or not may_touch(user, record.id):
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
    server: OneBotDep,
    users: UserStoreDep,
) -> ApiResponse[list[BotData]]:
    """机器人列表（**只有记录，明文拿不回来**）：非管理员只看得到自己那条。"""
    trace_id: str = trace_id_of(request)
    registry = server.tokens
    if registry is None:
        raise ApiError(
            ErrorCode.HTTP_ERROR,
            "没配令牌注册表",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    records = await registry.list_records(owner_id=None if is_admin(user) else user.user.id)
    names = await _nicknames_of(users, [record.id for record in records])
    data: list[BotData] = []
    for record in records:
        nickname = names.get(record.id, "")
        clients = server.roster(id=record.id)
        data.append(_bot_of(record, nickname, online=bool(clients), clients=clients))
    return ApiResponse[list[BotData]](data=data, trace_id=trace_id)


@router.post(
    "",
    response_model=ApiResponse[IssuedBotData],
    status_code=status.HTTP_200_OK,
    summary="添加机器人",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse, "description": "没登录"},
        status.HTTP_501_NOT_IMPLEMENTED: {"model": ErrorResponse, "description": "该平台暂未支持"},
    },
)
async def add_bot(
    payload: AddBotRequest,
    request: Request,
    user: CurrentUserDep,
    server: OneBotDep,
    users: UserStoreDep,
) -> ApiResponse[IssuedBotData]:
    """给当前用户添加一个机器人；明文令牌只在这一次响应里出现。

    platform=onebot：走现有令牌签发（一个归属一条，多实例在 P5-4 统一切）；
    platform=kook：P6 才接入，现在回 501。
    """
    trace_id: str = trace_id_of(request)
    if payload.platform != "onebot":
        raise ApiError(
            ErrorCode.HTTP_ERROR,
            f"平台 {payload.platform} 暂未接入（P6 开放）",
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
        )
    registry = server.tokens
    if registry is None:
        raise ApiError(
            ErrorCode.HTTP_ERROR, "没配令牌注册表", status_code=status.HTTP_404_NOT_FOUND
        )
    issued = await registry.issue(user.user.id, account=payload.account, remark=payload.remark)
    nickname = await _nickname_of(users, issued.record.id)
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
    server: OneBotDep,
    users: UserStoreDep,
) -> ApiResponse[BotData]:
    """启用 / 停用一个机器人；停用会断开它连着的客户端。"""
    trace_id: str = trace_id_of(request)
    record = await _ensure_in_scope(user, server, bot_id)
    changed = await server.set_token_enabled(bot_id, payload.enabled)
    if not changed:
        raise ApiError(ErrorCode.HTTP_ERROR, "没有这个机器人", status_code=status.HTTP_404_NOT_FOUND)
    nickname = await _nickname_of(users, record.id)
    clients = server.roster(id=record.id)
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
    server: OneBotDep,
) -> ApiResponse[dict[str, bool]]:
    """删除机器人：吊销令牌并把正用它连着的客户端断开。"""
    trace_id: str = trace_id_of(request)
    await _ensure_in_scope(user, server, bot_id)
    removed = await server.revoke_by_id(bot_id)
    if not removed:
        raise ApiError(ErrorCode.HTTP_ERROR, "没有这个机器人", status_code=status.HTTP_404_NOT_FOUND)
    return ApiResponse[dict[str, bool]](data={"removed": True}, trace_id=trace_id)
