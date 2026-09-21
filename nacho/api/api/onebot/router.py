"""OneBot 管理的 HTTP 入口：在线列表、踢人、令牌的签发与吊销。

    GET    <prefix>/onebot/clients          在线客户端（``?account=`` 只看某个账号下的）
    DELETE <prefix>/onebot/clients/{id}     踢下线（``?revoke=true`` 连令牌一起吊销）
    GET    <prefix>/onebot/tokens           令牌列表
    POST   <prefix>/onebot/tokens           签发令牌（明文只露这一次）
    PATCH  <prefix>/onebot/tokens/{id}      启用 / 停用（停用会断开用它连着的客户端）
    DELETE <prefix>/onebot/tokens/{id}      吊销令牌（并把用它连着的客户端断开）

这些都要登录：``Authorization: Bearer <token>``，令牌是 ``POST /auth/login`` 给的那个。

**登录了还不算完，还得看身份**：带 ``admin`` 角色的人不限（能管所有账号），其余人只能管
**自己账号**（``user.account``）名下那部分。判管理员用 :func:`is_admin`，能不能碰某账号由
:func:`may_touch` / :func:`ensure_can_touch` 把关。

**删一个客户端要走两条腿**：OneBot 实现断线都会自动重连，所以「从列表里删掉」= 断开这条
连接 **并且** 吊销它的令牌 —— 只做一半的话，过几秒它又会回到列表里。``DELETE /clients/{id}``
默认只踢，带 ``?revoke=true`` 才连令牌一起吊销；``DELETE /tokens/{id}`` 则是先吊销再断开。

路由本身不含业务判断：拿到服务调一下、按范围过滤一下，把结果装进响应模型。服务在哪、
令牌存哪由装配（:func:`nacho.api.create_app`）决定。
"""
from __future__ import annotations

from fastapi import APIRouter, Request, status

from ...common.dependencies import trace_id_of
from ...common.errors import ApiError, ErrorCode
from ...common.models import ApiResponse, ErrorResponse
from ...services.auth.models import CurrentUser
from .dependencies import (
    CurrentUserDep,
    OneBotDep,
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


def _client_of(item: ClientLike) -> ClientData:
    """把服务端的一行在线记录装成响应模型。"""
    return ClientData(
        id=item.id,
        account=item.account,
        self_id=item.self_id,
        remote=item.remote,
        connected_at=item.connected_at,
    )


def _token_of(record: TokenLike) -> TokenData:
    """把一条令牌记录装成响应模型（**不含明文**，库里存的本来也只有摘要）。"""
    return TokenData(
        id=record.id,
        account=record.account,
        enabled=record.enabled,
        remark=record.remark,
        created_at=record.created_at,
    )


async def _fetch_token(server: OneBotLike, token_id: str) -> TokenData:
    """按 id 取一条令牌记录（改完状态后回最新的那份给客户端）。"""
    registry = server.tokens
    if registry is None:
        raise ApiError(
            ErrorCode.HTTP_ERROR, "没配令牌注册表", status_code=status.HTTP_404_NOT_FOUND
        )
    record = await registry.get_by_id(token_id)
    if record is None:
        raise ApiError(ErrorCode.HTTP_ERROR, "没有这个令牌", status_code=status.HTTP_404_NOT_FOUND)
    return _token_of(record)


async def _ensure_token_in_scope(user: CurrentUser, server: OneBotLike, token_id: str) -> None:
    """按 id 找到令牌、确认在调用者的范围内；**找不到和不是自己的，走同一个 404**。

    404 而不是 403：403 等于告诉对方「这条 id 是存在的」，拿 id 就能试探出别人有几条令牌。
    """
    registry = server.tokens
    if registry is None:
        raise ApiError(
            ErrorCode.HTTP_ERROR,
            "没配令牌注册表：当前 OneBot 不校验，也没有令牌可管",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    record = await registry.get_by_id(token_id)
    if record is None or not may_touch(user, record.account):
        raise ApiError(ErrorCode.HTTP_ERROR, "没有这个令牌", status_code=status.HTTP_404_NOT_FOUND)


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
    account: str | None = None,
) -> ApiResponse[list[ClientData]]:
    """在线客户端（快照）；带 ``?account=xxx`` 只看那一个账号下的。

    非管理员不带 ``?account=`` 时**默认只看自己账号的**；显式要别人的 → 403（不是给个空列表，
    自己的范围就说清楚）。
    """
    trace_id: str = trace_id_of(request)
    if account is None:
        if not is_admin(user):
            account = user.user.account  # 不写就只看自己账号，别把别人的漏出去
    else:
        ensure_can_touch(user, account)
    data = [_client_of(item) for item in server.roster(account=account)]
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
    target = next((item for item in server.roster() if item.id == client_id), None)
    if target is None or not may_touch(user, target.account):
        raise ApiError(
            ErrorCode.HTTP_ERROR, "没有这个客户端", status_code=status.HTTP_404_NOT_FOUND
        )
    await server.kick(client_id, revoke=revoke)
    return ApiResponse[KickData](
        data=KickData(client_id=client_id, account=target.account, revoked=revoke),
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
) -> ApiResponse[list[TokenData]]:
    """令牌列表（**只有记录，明文拿不回来**）：非管理员只看得到自己账号下的那些。"""
    trace_id: str = trace_id_of(request)
    registry = server.tokens
    if registry is None:
        raise ApiError(
            ErrorCode.HTTP_ERROR,
            "没配令牌注册表：当前 OneBot 不校验，也没有令牌可管",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    # 范围过滤下推到注册表：非管理员只查自己账号那批，别人的行根本不读
    records = await registry.list_records(account=None if is_admin(user) else user.user.account)
    data = [_token_of(record) for record in records]
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
) -> ApiResponse[IssuedTokenData]:
    """给一个账号签令牌；**明文只在这一次响应里出现**，客户端要自己存好。

    把明文拿去配到 OneBot 实现的「反向 WS 地址」上（``Authorization: Bearer`` 或
    ``?access_token=``），它连进来就归到这个账号下。非管理员只能给自己账号签
    （``payload.account`` 是自己那个），否则 403。

    范围先判、再判有没有配注册表：**不先告诉一个无权的人这台机器的服务配成什么样**。
    """
    trace_id: str = trace_id_of(request)
    ensure_can_touch(user, payload.account)
    registry = server.tokens
    if registry is None:
        raise ApiError(
            ErrorCode.HTTP_ERROR,
            "没配令牌注册表：签了也没人认",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    issued = await registry.issue(payload.account, remark=payload.remark)
    return ApiResponse[IssuedTokenData](
        data=IssuedTokenData(record=_token_of(issued.record), token=issued.token),
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
) -> ApiResponse[TokenData]:
    """启用 / 停用一条令牌；停用它连着的客户端会一并断开，且重连被 401 拒。

    和 ``DELETE``（吊销）的区别：记录还在、随时能启用回来，吊销则是删掉、不可逆。
    别人的令牌按「没有这个令牌」处理（404）。
    """
    trace_id: str = trace_id_of(request)
    await _ensure_token_in_scope(user, server, token_id)
    changed = await server.set_token_enabled(token_id, payload.enabled)
    if not changed:
        raise ApiError(
            ErrorCode.HTTP_ERROR, "没有这个令牌", status_code=status.HTTP_404_NOT_FOUND
        )
    return ApiResponse[TokenData](data=await _fetch_token(server, token_id), trace_id=trace_id)


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
    await _ensure_token_in_scope(user, server, token_id)
    removed = await server.revoke_by_id(token_id)
    if not removed:
        raise ApiError(
            ErrorCode.HTTP_ERROR, "没有这个令牌", status_code=status.HTTP_404_NOT_FOUND
        )
    return ApiResponse[RevokeData](
        data=RevokeData(token_id=token_id, removed=True), trace_id=trace_id
    )
