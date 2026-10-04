"""Message admission for cooperating consumers; uses bot credentials, not UI login sessions.

The dispatcher is injected by bootstrap: this API does not import platform/workflow implementations.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from ..common.dependencies import trace_id_of
from ..common.errors import ApiError, ErrorCode
from ..common.models import ApiResponse
from .onebot.dependencies import OneBotDep


class DispatchMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    self_id: str = Field(min_length=1, max_length=128, pattern=r"^[0-9]+$")
    message_id: str = Field(min_length=1, max_length=128, pattern=r"^-?[0-9]+$")
    user_id: str = Field(min_length=1, max_length=128, pattern=r"^[0-9]+$")
    chat: Literal["group", "private"]
    chat_id: str = Field(min_length=1, max_length=128, pattern=r"^[0-9]+$")
    message: str = Field(max_length=65536)


class DispatchMessageResult(BaseModel):
    consumed: bool


MessageDispatcher = Callable[[str, DispatchMessageRequest], Awaitable[bool]]
router = APIRouter(prefix="/messages", tags=["消息接管"])


@router.post("/dispatch", response_model=ApiResponse[DispatchMessageResult])
async def dispatch_message(body: DispatchMessageRequest, request: Request,
                           server: OneBotDep) -> ApiResponse[DispatchMessageResult]:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    record = (await server.tokens.resolve(token)) if server.tokens and scheme.lower() == "bearer" else None
    if record is None or record.platform != "onebot":
        raise ApiError(ErrorCode.UNAUTHORIZED, "机器人凭证无效", status_code=401)
    clients = server.roster(id=record.owner_id)
    if not any(client.bot_id == record.id and str(client.self_id) == body.self_id for client in clients):
        raise ApiError(ErrorCode.HTTP_ERROR, "凭证与在线机器人账号不匹配", status_code=409)
    if body.user_id == body.self_id:
        raise ApiError(ErrorCode.HTTP_ERROR, "不能把机器人自身输出作为新消息分发", status_code=422)
    if body.chat == "private" and body.chat_id != body.user_id:
        raise ApiError(ErrorCode.HTTP_ERROR, "私聊目标与发消息的人不一致", status_code=422)
    dispatcher: MessageDispatcher | None = getattr(request.app.state, "message_dispatcher", None)
    if dispatcher is None:
        raise ApiError(ErrorCode.HTTP_ERROR, "消息接管未接入", status_code=503)
    try:
        consumed = await asyncio.wait_for(dispatcher(record.owner_id, body), timeout=25)
    except TimeoutError as exc:
        # The router shields its execution. Never launch a second consumer while this is pending.
        raise ApiError(ErrorCode.HTTP_ERROR, "工作流仍在处理；本次未交给 AI，工作流不会被取消", status_code=504) from exc
    return ApiResponse(data=DispatchMessageResult(consumed=consumed), trace_id=trace_id_of(request))
