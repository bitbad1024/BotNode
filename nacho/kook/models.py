"""Kook 正向 WS 的数据形状：网关推下来的事件（signal 0）与 pong（signal 3）。

Kook 网关的报文是 ``{"s": <signal>, "d": {...}}``：

* ``s`` 是 signal 编号：0 = 事件、1 = hello、2 = ping、3 = pong（下个心跳用的 sn）；
* ``d`` 是载荷，事件里 ``type`` 是事件类型（1 = 文字消息、9 = 频道消息、255 = 系统事件等）。

这里把「事件」定成 pydantic 模型（:class:`KookEvent`），``extra="allow"`` —— Kook 不同
频道类型（文字 / 语音 / 私聊）带的字段不一样，不认识的照单收下（仍可从 ``model_extra``
读到），不会因为多了一个键就解析失败。

身份字段在 Kook 里都是**字符串**（channel_id / user_id / msg_id 都是），与 OneBot 的整数
不同 —— 翻译成 :class:`~nacho.bridge.models.PlatformEvent` 时字符串口径正好对得上。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, TypeAdapter


class _Base(BaseModel):
    """事件的公共字段。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="allow")


class KookEvent(_Base):
    """一条 Kook 事件（signal 0 的载荷 ``d``）。

    字段都是 Kook 网关原样给的（缺省给空值兜底，避免某类事件不带某字段时解析失败）：
    """

    #: 事件类型：1 = 文字消息、2 = 图片消息、9 = 频道消息、255 = 系统事件 …（Kook 网关的约定）
    type: int = 0
    #: 频道类型：GROUP（文字频道）/ PERSON（私聊）/ BROADCAST（广播频道）…；事件不同可能没有
    channel_type: str = ""
    #: 目标 id：文字消息是 channel_id，私聊是对方 user_id
    target_id: str = ""
    #: 作者 id（发消息的人）
    author_id: str = ""
    #: 消息 id
    msg_id: str = ""
    #: 消息正文（纯文本；图片消息等可能是别的形状）
    content: str = ""
    #: 消息时间戳（Unix 毫秒，Kook 惯例）
    msg_timestamp: int = 0
    #: 机器人自身 id（部分事件带）
    self_id: str = ""
    #: 事件附带的结构化字段（Kook 报文的 ``extra`` 对象）：没在这里声明，extra="allow"
    #: 下原样收进 ``model_extra``（键就叫 ``extra``），翻译要用时从 ``model_extra["extra"]`` 下探


#: Kook 事件里 `d.type` 的关键取值（文字 / 图片 / 频道消息 / 系统事件）
EVENT_TEXT: int = 1
EVENT_IMAGE: int = 2
EVENT_CHANNEL: int = 9
EVENT_SYSTEM: int = 255


#: 事件解析适配器：Kook 事件就一种形状（字段全带默认值兜底），直接解析 ``d``
_EVENT_ADAPTER: TypeAdapter[KookEvent] = TypeAdapter(KookEvent)


def parse_event(payload: Mapping[str, object]) -> KookEvent:
    """把 signal 0 的载荷 ``d`` 解析成事件模型。

    :raises pydantic.ValidationError: 形状不对（缺 ``type`` 等）。
    """
    return _EVENT_ADAPTER.validate_python(payload)


class KookActionResponse(_Base):
    """动作回应（Kook 的 REST 回应形状）。

    Kook 的正向 WS 只推送事件，发消息走 **REST API**（HTTP POST），回应是 ``{"code": 0,
    "message": "success", "data": {...}}``。这里定成与 OneBot ``ActionResponse`` 同构的
    语义：``ok`` 由 ``code == 0`` 判定。
    """

    #: 业务码：0 = 成功，非 0 = 失败（Kook 的惯例）
    code: int = 0
    #: 说明文字（失败时是错误原因）
    message: str = "success"
    #: 附带结果
    data: object = None

    @property
    def ok(self) -> bool:
        """成功：``code == 0``。"""
        return self.code == 0


def parse_action_response(payload: Mapping[str, object]) -> KookActionResponse:
    """把 REST 回应解析成 :class:`KookActionResponse`。"""
    return KookActionResponse.model_validate(payload)
