"""规范化数据形状：跨平台统一的事件 / 在线列表 / 动作回执。

为什么要有这一层形状：各平台的事件模型差异很大（OneBot 的 ``user_id`` 是整数、
Kook 的是字符串），下游（workflow 触发、日志钩子）不该逐平台认字段。适配器把
平台事件**翻译**成这里的 :class:`PlatformEvent`，下游只认这一份。

口径（谁定的谁维护）：

* 身份一律**字符串**：``owner_id``（谁的，与令牌记录同一套 id 空间）、``self_id``
  （机器人自身账号）、``user_id`` / ``chat_id``（对方 / 会话）。数字平台的适配器
  负责转成字符串；
* 「归属」跨平台不再唯一：同一套 ``owner_id`` 在不同平台各有一条连接，所以
  :class:`PlatformEvent` 带着 ``platform``，Gateway 内部按 ``(platform, owner_id)``
  复合键路由。**本阶段复合键不落库、不进接口层**（令牌仍是 onebot 专属，Kook 的
  令牌形态留给 P4）；
* 翻译不了的字段不去硬翻：平台原始事件整条挂在 ``raw`` 上（OneBot 来的就是
  :class:`~nacho.platforms.onebot.models.OneBotEvent`），下游要用细节就下探到 ``raw``，
  但下探就意味着绑平台 —— 能用规范化字段就别用 ``raw``。

三个模型都是**冻结**的：事件是已发生的事，不改写。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypeAlias

#: 事件大类：消息 / 通知 / 请求 / 元（连接生命周期、心跳一类）
EventKind: TypeAlias = Literal["message", "notice", "request", "meta"]

#: 会话指向：群聊 / 私聊（单聊）/ 其它（没有明确会话概念的事件，如纯通知）
ChatKind: TypeAlias = Literal["group", "private", "other"]


@dataclass(frozen=True)
class PlatformEvent:
    """一条规范化事件：平台适配器翻译出来、投给 Gateway 的形状。

    字段分三组看：

    * 身份 —— ``platform`` / ``owner_id`` / ``self_id``：哪个平台、谁的机器人、机器人自己是谁；
    * 指向 —— ``kind`` / ``chat`` / ``user_id``：什么类型的事、发生在哪个会话、对方是谁；
    * 内容 —— ``text`` / ``message_id`` / ``raw``：正文、消息号、原始事件兜底。

    ``text`` 只在消息事件里有意义（其余为空串），且是**纯文本正文**（OneBot 的
    ``raw_message``）；要消息段数组、sender 详情这些就下探 ``raw``。
    """

    #: 事件来源平台（适配器的 ``platform`` 标识，如 ``"onebot"``）
    platform: str
    #: 这条连接属于谁（握手时令牌定下的 ``id``；匿名连接是空串）
    owner_id: str
    #: 机器人自身账号（OneBot 是 QQ 号；没学到时是空串）
    self_id: str = ""
    #: 事件大类
    kind: EventKind = "meta"
    #: 会话指向：群还是私聊；没有明确会话的事件（纯通知）是 ``"other"``
    chat: ChatKind = "other"
    #: 会话标识：群号（群聊）或对方账号（私聊）；没有是空串
    chat_id: str = ""
    #: 对方用户账号；与事件无关（如心跳）时是空串
    user_id: str = ""
    #: 纯文本正文（仅消息事件；其余空串）
    text: str = ""
    #: 消息号（撤回一类动作要用）；没有是空串
    message_id: str = ""
    #: 事件发生时间（Unix 秒）；平台没给时是 0
    time: float = 0.0
    #: 平台原始事件（整条引用，形状随平台）；翻译不了的字段从这里兜
    raw: object = field(default=None, repr=False)


@dataclass(frozen=True)
class BotClient:
    """在线列表里的一行：某个平台下、连着的一条机器人连接。

    与 :class:`~nacho.platforms.onebot.server.ClientEntry` 同构，但身份字段统一成字符串口径，
    且**不带**平台字段 —— 在线列表总是从某个适配器问出来（``adapter.clients()``），
    「哪个平台」由问谁决定。
    """

    #: 这条连接自己的编号（踢人时按它定位）
    client_id: str
    #: 这条连接属于谁（没配令牌注册表时是空串 = 匿名）
    owner_id: str
    #: 令牌里带的机器人账号（用户填的，展示用）
    account: str = ""
    #: 机器人号（收到第一条事件才学到；没学到是空串）
    self_id: str = ""
    #: 对端地址（形如 ``127.0.0.1:53210``）
    remote: str = ""
    #: 连上的时刻（Unix 秒）
    connected_at: float = 0.0


@dataclass(frozen=True)
class ActionResult:
    """一个动作发出去之后的回执（规范化）。

    与平台的回执模型（OneBot 的 ``ActionResponse``）分工：适配器把平台回执翻译成
    这一份，下游先看 ``ok`` / ``message`` 决定成败；要平台细节（retcode、原始 data）
    再下探 ``raw``。

    **两种失败分开**（与 onebot 节点同一口径）：环境问题（没连接、没这个平台）由
    适配器当场抛异常，走不到这里；这里是「发出去、对方答了不成功」。
    """

    #: 对方是否收下并执行成功
    ok: bool
    #: 失败说明（成功时是空串）
    message: str = ""
    #: 平台附带的结果数据（成功时才有意义；形状随平台）
    data: object = field(default=None, repr=False)
    #: 平台原始回执（整条引用；形状随平台）
    raw: object = field(default=None, repr=False)
