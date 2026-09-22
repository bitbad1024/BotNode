"""OneBot 管理的 HTTP 入口：在线列表、踢人、令牌的签发与吊销。

    GET    <prefix>/onebot/clients          在线客户端（``?id=`` 只看某个归属下的）
    DELETE <prefix>/onebot/clients/{id}     踢下线（``?revoke=true`` 连令牌一起吊销）
    GET    <prefix>/onebot/tokens           令牌列表（带归属昵称）
    POST   <prefix>/onebot/tokens           签发令牌（给当前登录用户；明文只露这一次）
    PATCH  <prefix>/onebot/tokens/{id}      启用 / 停用（停用会断开用它连着的客户端）
    DELETE <prefix>/onebot/tokens/{id}      吊销令牌（并把用它连着的客户端断开）

这些都要登录：``Authorization: Bearer <token>``，令牌是 ``POST /auth/login`` 给的那个。

**归属就是令牌记录的 ``id``**（谁的）：语义本层不解释，接口层只拿它去用户表查昵称。
**登录了还不算完，还得看身份**：带 ``admin`` 角色的人不限（能管所有归属），其余人只能管
**自己那个 id** 名下那部分。判管理员用 :func:`is_admin`，能不能碰某个归属由
:func:`may_touch` / :func:`ensure_can_touch` 把关。

**签发不用填归属**：永远签给当前登录用户（``user.user.id``）；请求体里的 ``account`` 是
「接入 WS 的机器人账号」，自由填，只用来展示。**一个归属一条**，再签就是换一把钥匙。

**删一个客户端要走两条腿**：OneBot 实现断线都会自动重连，所以「从列表里删掉」= 断开这条
连接 **并且** 吊销它的令牌 —— 只做一半的话，过几秒它又会回到列表里。``DELETE /clients/{id}``
默认只踢，带 ``?revoke=true`` 才连令牌一起吊销；``DELETE /tokens/{id}`` 则是先吊销再断开。

路由本身不含业务判断：拿到服务调一下、按范围过滤一下，把结果装进响应模型。服务在哪、
令牌存哪由装配（:func:`nacho.api.create_app`）决定。
"""
from __future__ import annotations

from collections.abc import Iterable

from fastapi import APIRouter, Request, status

from ...common.dependencies import trace_id_of
from ...common.errors import ApiError, ErrorCode
from ...common.models import ApiResponse, ErrorResponse
from ...logging import api_logger
from ...services.auth.models import CurrentUser
from ...services.user.protocols import UserStore
from .dependencies import (
    CurrentUserDep,
    OneBotDep,
    UserStoreDep,
    ensure_can_touch,
    is_admin,
    may_touch,
)
from .protocols import ClientLike, OneBotLike, TokenLike
from .requests import IssueTokenRequest, SetTokenEnabledRequest
from .responses import (
    ClientData,
    IssuedTokenData,
    KickData,
    RevokeData,
    TokenData,
)

router = APIRouter(prefix="/onebot", tags=["OneBot 管理"])


def _client_of(item: ClientLike, nickname: str) -> ClientData:
    """把服务端的一行在线记录装成响应模型（归属带上昵称，和令牌列表一个口径）。"""
    return ClientData(
        client_id=item.client_id,
        id=item.id,
        nickname=nickname,
        account=item.account,
        self_id=item.self_id,
        remote=item.remote,
        connected_at=item.connected_at,
    )


async def _nicknames_of(users: UserStore, owner_ids: Iterable[str]) -> dict[str, str]:
    """**一次**把一批归属 ``id`` 的昵称查齐（``id -> 昵称``）：列表页别一条一条来（N+1）。

    ``id`` 的语义本层不解释——不是用户的 id 就不会出现在结果里，调用方按空串回落。
    """
    records = await users.get_by_ids(owner_ids)
    return {owner_id: record.nickname for owner_id, record in records.items()}


async def _nickname_of(users: UserStore, owner_id: str) -> str:
    """只装一条记录时用：拿归属 ``id`` 查昵称；查不到（这个 id 不是用户）就空串。"""
    return (await _nicknames_of(users, [owner_id])).get(owner_id, "")


def _token_of(record: TokenLike, nickname: str) -> TokenData:
    """把一条令牌记录装成响应模型（**不含明文**，库里存的本来也只有摘要）。"""
    return TokenData(
        id=record.id,
        account=record.account,
        enabled=record.enabled,
        remark=record.remark,
        created_at=record.created_at,
        nickname=nickname,
    )


async def _fetch_token(server: OneBotLike, users: UserStore, token_id: str) -> TokenData:
    """按 id 取一条令牌记录（改完状态后回最新的那份给客户端）。"""
    registry = server.tokens
    if registry is None:
        raise ApiError(
            ErrorCode.HTTP_ERROR, "没配令牌注册表", status_code=status.HTTP_404_NOT_FOUND
        )
    record = await registry.get_by_id(token_id)
    if record is None:
        raise ApiError(ErrorCode.HTTP_ERROR, "没有这个令牌", status_code=status.HTTP_404_NOT_FOUND)
    return _token_of(record, await _nickname_of(users, record.id))


async def _ensure_token_in_scope(user: CurrentUser, server: OneBotLike, token_id: str) -> TokenLike:
    """按 id 找到令牌、确认在调用者的范围内；**找不到和不是自己的，走同一个 404**。

    404 而不是 403：403 等于告诉对方「这条 id 是存在的」，拿 id 就能试探出别人有没有令牌。

    :return: 找到的那条令牌记录（审计日志要拿它的归属与机器人账号）。
    """
    registry = server.tokens
    if registry is None:
        raise ApiError(
            ErrorCode.HTTP_ERROR,
            "没配令牌注册表：当前 OneBot 不校验，也没有令牌可管",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    record = await registry.get_by_id(token_id)
    if record is None or not may_touch(user, record.id):
        raise ApiError(ErrorCode.HTTP_ERROR, "没有这个令牌", status_code=status.HTTP_404_NOT_FOUND)
    return record


def _audit(message: str, *, owner_id: str, **extra: object) -> None:
    """记一条令牌审计事件（``api`` 那路，落库那份就是审计时间线）。

    ``owner_id`` 归**令牌的主人**（记录 id 即归属用户 id）：谁名下的钥匙被动了，谁就
    该在自己的日志里看到这条；动手的人在 ``extra["actor"]`` 里。
    """
    api_logger().info(message, owner_id=owner_id, **extra)


@router.get(
    "/clients",
    response_model=ApiResponse[list[ClientData]],
    summary="在线客户端列表",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse, "description": "没登录 / 令牌无效"}
    },
)
async def list_clients(
    request: Request,
    user: CurrentUserDep,  # 先鉴权：没登录就 401，不往外说服务接没接
    server: OneBotDep,
    users: UserStoreDep,
    id: str | None = None,
) -> ApiResponse[list[ClientData]]:
    """在线客户端（快照）；带 ``?id=xxx`` 只看那一个归属（谁的）下的。

    非管理员不带 ``?id=`` 时**默认只看自己的**；显式要别人的 → 403（不是给个空列表，
    自己的范围就说清楚）。每条带归属昵称（和令牌列表一个口径）。
    """
    trace_id: str = trace_id_of(request)
    if id is None:
        if not is_admin(user):
            id = user.user.id  # 不写就只看自己的，别把别人的漏出去
    else:
        ensure_can_touch(user, id)
    entries = server.roster(id=id)
    # 昵称一次查齐（别一条一次查询）：查不到的 id 回落空串
    names = await _nicknames_of(users, [item.id for item in entries])
    data = [_client_of(item, names.get(item.id, "")) for item in entries]
    return ApiResponse[list[ClientData]](data=data, trace_id=trace_id)


@router.delete(
    "/clients/{client_id}",
    response_model=ApiResponse[KickData],
    summary="把一个客户端踢下线",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse, "description": "没登录 / 令牌无效"},
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse, "description": "没有这个客户端"},
    },
)
async def kick_client(
    client_id: str,
    request: Request,
    user: CurrentUserDep,
    server: OneBotDep,
    revoke: bool = False,
) -> ApiResponse[KickData]:
    """踢下线；``?revoke=true`` 连它的令牌一起吊销（否则它会自动重连回列表里）。

    别人的客户端按「**没有这个客户端**」处理（404 而不是 403）：按 id 找东西时给 403 等于
    告诉对方「这条 id 是存在的」，拿 id 就能试探出别人有多少条连接。
    """
    trace_id: str = trace_id_of(request)
    target = next((item for item in server.roster() if item.client_id == client_id), None)
    if target is None or not may_touch(user, target.id):
        raise ApiError(
            ErrorCode.HTTP_ERROR, "没有这个客户端", status_code=status.HTTP_404_NOT_FOUND
        )
    await server.kick(client_id, revoke=revoke)
    return ApiResponse[KickData](
        data=KickData(client_id=client_id, id=target.id, revoked=revoke),
        trace_id=trace_id,
    )


@router.get(
    "/tokens",
    response_model=ApiResponse[list[TokenData]],
    summary="令牌列表",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse, "description": "没登录 / 令牌无效"},
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorResponse,
            "description": "没配令牌注册表（当前 OneBot 不校验，也没有令牌可管）",
        },
    },
)
async def list_tokens(
    request: Request,
    user: CurrentUserDep,
    server: OneBotDep,
    users: UserStoreDep,
) -> ApiResponse[list[TokenData]]:
    """令牌列表（**只有记录，明文拿不回来**）：非管理员只看得到自己那条。

    每条带上归属的昵称（拿 ``id`` 去用户表查，查不到就空串）。
    """
    trace_id: str = trace_id_of(request)
    registry = server.tokens
    if registry is None:
        raise ApiError(
            ErrorCode.HTTP_ERROR,
            "没配令牌注册表：当前 OneBot 不校验，也没有令牌可管",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    # 范围过滤下推到注册表：非管理员只查自己那条，别人的行根本不读
    records = await registry.list_records(owner_id=None if is_admin(user) else user.user.id)
    # 昵称一次查齐（别一条一次查询）：查不到的 id 回落空串
    names = await _nicknames_of(users, [record.id for record in records])
    data = [_token_of(record, names.get(record.id, "")) for record in records]
    return ApiResponse[list[TokenData]](data=data, trace_id=trace_id)


@router.post(
    "/tokens",
    response_model=ApiResponse[IssuedTokenData],
    status_code=status.HTTP_200_OK,
    summary="签发一个令牌",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse, "description": "没登录 / 令牌无效"},
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorResponse,
            "description": "没配令牌注册表：签了也没人认",
        },
    },
)
async def issue_token(
    payload: IssueTokenRequest,
    request: Request,
    user: CurrentUserDep,
    server: OneBotDep,
    users: UserStoreDep,
) -> ApiResponse[IssuedTokenData]:
    """给**当前登录用户**签令牌；**明文只在这一次响应里出现**，客户端要自己存好。

    把明文拿去配到 OneBot 实现的「反向 WS 地址」上（``Authorization: Bearer`` 或
    ``?access_token=``），它连进来就归到当前用户下。归属填不了——就是自己。

    **一个归属一条**：同一用户再签等于换一把钥匙（旧令牌立刻失效、旧连接下次握手就认不出来）。
    """
    trace_id: str = trace_id_of(request)
    registry = server.tokens
    if registry is None:
        raise ApiError(
            ErrorCode.HTTP_ERROR,
            "没配令牌注册表：签了也没人认",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    issued = await registry.issue(user.user.id, account=payload.account, remark=payload.remark)
    _audit(
        "WS 令牌已签发",
        owner_id=issued.record.id,
        token_id=issued.record.id,
        account=issued.record.account,
        actor=user.user.id,
        trace_id=trace_id,
    )
    return ApiResponse[IssuedTokenData](
        data=IssuedTokenData(
            record=_token_of(issued.record, await _nickname_of(users, issued.record.id)),
            token=issued.token,
        ),
        trace_id=trace_id,
    )


@router.patch(
    "/tokens/{token_id}",
    response_model=ApiResponse[TokenData],
    summary="启用 / 停用令牌",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse, "description": "没登录 / 令牌无效"},
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse, "description": "没有这个令牌"},
    },
)
async def set_token_enabled(
    token_id: str,
    payload: SetTokenEnabledRequest,
    request: Request,
    user: CurrentUserDep,
    server: OneBotDep,
    users: UserStoreDep,
) -> ApiResponse[TokenData]:
    """启用 / 停用一条令牌；停用它连着的客户端会一并断开，且重连被 401 拒。

    和 ``DELETE``（吊销）的区别：记录还在、随时能启用回来，吊销则是删掉、不可逆。
    别人的令牌按「没有这个令牌」处理（404）。
    """
    trace_id: str = trace_id_of(request)
    record = await _ensure_token_in_scope(user, server, token_id)
    changed = await server.set_token_enabled(token_id, payload.enabled)
    if not changed:
        raise ApiError(
            ErrorCode.HTTP_ERROR, "没有这个令牌", status_code=status.HTTP_404_NOT_FOUND
        )
    _audit(
        "WS 令牌已启用" if payload.enabled else "WS 令牌已停用",
        owner_id=record.id,
        token_id=token_id,
        account=record.account,
        actor=user.user.id,
        trace_id=trace_id,
    )
    return ApiResponse[TokenData](
        data=await _fetch_token(server, users, token_id), trace_id=trace_id
    )


@router.delete(
    "/tokens/{token_id}",
    response_model=ApiResponse[RevokeData],
    summary="吊销一个令牌",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse, "description": "没登录 / 令牌无效"},
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse, "description": "没有这个令牌"},
    },
)
async def revoke_token(
    token_id: str,
    request: Request,
    user: CurrentUserDep,
    server: OneBotDep,
) -> ApiResponse[RevokeData]:
    """吊销令牌，并把正用它连着的客户端断开（从在线列表里消失、也重连不回来）。

    别人的令牌按「没有这个令牌」处理（404）。
    """
    trace_id: str = trace_id_of(request)
    record = await _ensure_token_in_scope(user, server, token_id)
    removed = await server.revoke_by_id(token_id)
    if not removed:
        raise ApiError(
            ErrorCode.HTTP_ERROR, "没有这个令牌", status_code=status.HTTP_404_NOT_FOUND
        )
    _audit(
        "WS 令牌已吊销",
        owner_id=record.id,
        token_id=token_id,
        account=record.account,
        actor=user.user.id,
        trace_id=trace_id,
    )
    return ApiResponse[RevokeData](
        data=RevokeData(token_id=token_id, removed=True), trace_id=trace_id
    )
