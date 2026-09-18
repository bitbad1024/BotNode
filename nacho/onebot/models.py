"""OneBot v11 的数据形状：上报事件与动作回应。

一条从客户端来的 JSON，要么是**事件**（带 ``post_type``），要么是**动作回应**（带 ``status`` /
``retcode``，以及发起动作时带的 ``echo``）。这里把两样都定成 pydantic 模型：

* :data:`OneBotEvent` 是按 ``post_type`` 判别的联合（message / notice / request / meta_event），
  用 :func:`parse_event` 解析；
* :class:`ActionResponse` 是动作回应，用 :func:`parse_action_response` 解析；
* :func:`is_event` 判断一份报文属于哪一类。

模型都开了 ``extra="allow"``：各家实现（go-cqhttp / NapCat / LLOneBot …）会带各自额外的字段，
不认识的照单收下（仍可从 ``model_extra`` 读到），不会因为多了一个键就解析失败。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, ClassVar, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class _Base(BaseModel):
    """三类事件的公共字段。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="allow")

    #: 事件发生时间（Unix 秒）
    time: int = 0
    #: 收到这条事件的机器人 id（即机器人 QQ 号）
    self_id: int = 0


class MessageEvent(_Base):
    """消息事件：私聊 / 群聊来了消息。"""

    post_type: Literal["message"] = "message"
    message_type: Literal["private", "group"] = "private"
    sub_type: str = ""
    message_id: int | None = None
    user_id: int = 0
    group_id: int | None = None
    #: 纯文本形式的正文（各实现都会给，最常用）
    raw_message: str = ""
    #: 原始消息（字符串或消息段数组），形状随实现
    message: object = None
    #: 发送者信息（昵称 / 群名片等），形状随实现
    sender: Mapping[str, object] = Field(default_factory=dict)


class NoticeEvent(_Base):
    """通知事件：群成员增减、戳一戳等。"""

    post_type: Literal["notice"] = "notice"
    notice_type: str = ""
    user_id: int | None = None
    group_id: int | None = None


class RequestEvent(_Base):
    """请求事件：加好友 / 加群请求。"""

    post_type: Literal["request"] = "request"
    request_type: str = ""
    user_id: int | None = None
    group_id: int | None = None
    comment: str = ""
    flag: str = ""


class MetaEvent(_Base):
    """元事件：生命周期（连上 / 断开）与心跳。"""

    post_type: Literal["meta_event"] = "meta_event"
    meta_event_type: Literal["lifecycle", "heartbeat"] = "heartbeat"
    sub_type: str = ""
    interval: int | None = None
    status: Mapping[str, object] | None = None


#: 一条上报事件：按 ``post_type`` 判别的联合
OneBotEvent: TypeAlias = Annotated[
    MessageEvent | NoticeEvent | RequestEvent | MetaEvent,
    Field(discriminator="post_type"),
]

_EVENT_ADAPTER: TypeAdapter[OneBotEvent] = TypeAdapter(OneBotEvent)


def is_event(payload: Mapping[str, object]) -> bool:
    """这份报文是**事件**（带 ``post_type``）还是**动作回应**。"""
    return "post_type" in payload


def parse_event(payload: Mapping[str, object]) -> OneBotEvent:
    """把一条上报报文解析成事件模型。

    :raises pydantic.ValidationError: 形状不对（缺字段 / ``post_type`` 不认）。
    """
    return _EVENT_ADAPTER.validate_python(payload)


class ActionResponse(BaseModel):
    """动作回应：``status`` / ``retcode`` 说明成败，``data`` 是附带结果。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="allow")

    status: str = "ok"
    retcode: int = 0
    data: object = None
    #: 发起动作时带的编号，用来把回应认回是哪一次调用
    echo: str | None = None

    @property
    def ok(self) -> bool:
        """成功：``status == "ok"`` 且 ``retcode == 0``。"""
        return self.status == "ok" and self.retcode == 0


def parse_action_response(payload: Mapping[str, object]) -> ActionResponse:
    """把一条动作回应解析成 :class:`ActionResponse`。"""
    return ActionResponse.model_validate(payload)
