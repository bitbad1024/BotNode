"""Kook 适配器：把 :class:`~tickneko.platforms.kook.KookClient` 包成第二个 ``BotAdapter``（**多客户端**）。

OneBot 是反向 WS（框架当服务端），Kook 是正向 WS（框架当客户端），方向相反却要塞进同一个
:class:`Gateway` —— 用来验证 ``BotAdapter`` 协议是不是真通用。三处与 OneBot 不同：**归属
语义**（一个 Bot Token 就是一个机器人，一个适配器管多个客户端 ``dict[bot_id, KookClient]``，
Gateway 里仍只占一个 platform 槽位）、**事件翻译**（``channel_type`` -> ``chat``、
``target_id`` -> ``chat_id``、``author_id`` -> ``user_id``、``content`` -> ``text``）、
**能力转述**（``send`` 走 REST ``KookClient.call``）。只依赖 ``tickneko.core``（logger）
与 ``tickneko.platforms.kook``。详见 ``docs/bridge/bridge.md``。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from tickneko.core.logger import BaseLogger
from tickneko.platforms.kook import (
    EVENT_SYSTEM,
    KookActionResponse,
    KookClient,
    KookEvent,
    KookOptions,
)

from .gateway import EventSubscriber
from .logging import bridge_logger
from .models import ActionResult, BotClient, ChatKind, ChatTarget, PlatformEvent

#: 本适配器的平台标识（路由键；Gateway 里不得与其它适配器重复）
PLATFORM = "kook"


@dataclass(frozen=True)
class KookTarget:
    """Kook 的会话定位（回程地址）：id 一律**字符串**（Kook 协议口径）。

    与 OneBot 的整数号相反，Kook 的频道号 / 对方号 / 消息号都是字符串，target 直接
    原样存，reply 时原样用。生产与消费同平台（见 :class:`ChatTarget`）：本类是适配器
    翻译事件时构造、塞进 ``PlatformEvent.target`` 的，reply 时原样传回本适配器。
    """

    #: 这条连接属于谁（回复发给「谁的」机器人）
    owner_id: str
    #: 事件来源平台（回复时按它路由回原适配器）
    platform: str = PLATFORM
    #: 会话指向：群聊（频道）/ 私聊（DM）；``"other"`` 说明定位不出会话，回不了
    chat: ChatKind = "other"
    #: 会话标识：频道号（群聊）或对方账号（私聊）；没有是空串
    chat_id: str = ""
    #: 对方用户账号；没有是空串
    user_id: str = ""
    #: 消息号（撤回一类动作要用）；没有是空串
    message_id: str = ""


def _translate(event: KookEvent, *, owner_id: str = "") -> PlatformEvent:
    """一条 Kook 事件 -> 规范化事件（身份转字符串，翻译不了的字段挂 ``raw`` 兜底）。

    ``owner_id`` 由适配器按 bot 的归属（凭证行 ``owner_id``）注入：这是「谁的机器人」，
    与 OneBot 的口径一致（框架归属用户 id）；``self_id`` 才是机器人平台账号。两者语义分开。
    """
    # Kook 的 channel_type：GROUP / PERSON / BROADCAST；只有前两者有明确会话指向
    channel_type = (event.channel_type or "").upper()
    if channel_type == "GROUP":
        chat = "group"
        chat_id = event.target_id
    elif channel_type == "PERSON":
        chat = "private"
        chat_id = event.target_id
    else:
        chat = "other"
        chat_id = ""

    # Kook 系统事件（type=255 系统消息 / author_id=1 系统账号）不是聊天消息：
    # 内容不在 content 而在 extra，不该进消息路由触发工作流。归为 notice（静默），
    # 身份照译、正文留空，下游（bootstrap）见非 message 只记 debug 不触发。
    if event.type == EVENT_SYSTEM or event.author_id == "1":
        # 系统事件没有会话指向（chat=other）：target 留 None，回不了也不用回
        return PlatformEvent(
            platform=PLATFORM,
            owner_id=owner_id,
            self_id=event.self_id or "",
            kind="notice",
            chat="other",
            chat_id="",
            user_id=event.author_id,
            text="",
            message_id=event.msg_id,
            time=float(event.msg_timestamp) / 1000.0 if event.msg_timestamp else 0.0,
            raw=event,
        )

    return PlatformEvent(
        platform=PLATFORM,
        owner_id=owner_id,  # 归属 = 框架用户（凭证行 owner_id，与 OneBot 同口径）
        self_id=event.self_id or "",
        kind="message",
        chat=chat,
        chat_id=chat_id,
        user_id=event.author_id,
        text=event.content,
        message_id=event.msg_id,
        time=float(event.msg_timestamp) / 1000.0 if event.msg_timestamp else 0.0,
        raw=event,
        target=KookTarget(
            owner_id=owner_id,
            chat=chat,
            chat_id=chat_id,
            user_id=event.author_id,
            message_id=event.msg_id,
        ),
    )


def _bot_options(template: KookOptions, token: str) -> KookOptions:
    """按模板 + Bot Token 拼一份客户端选项：网关 / 心跳 / 超时沿用模板，凭证用各自的 Token。"""
    return KookOptions(
        gateway=template.gateway,
        token=token,
        secret_key="",
        heartbeat_interval=template.heartbeat_interval,
        heartbeat_jitter=template.heartbeat_jitter,
        action_timeout=template.action_timeout,
        reconnect_interval=template.reconnect_interval,
        reconnect_max_interval=template.reconnect_max_interval,
        rest_min_interval=template.rest_min_interval,
        rest_max_retries=template.rest_max_retries,
    )


class KookAdapter:
    """Kook 平台适配器：管多个 ``KookClient``（``bot_id -> client``），实现 ``BotAdapter`` 协议。

    客户端是**按 bot 自建的**（``add_bot`` 时把 ``handler=self._handler_for(bot_id)`` 装进去，
    让事件能归到对应的机器人），连接的生命周期（起 / 停）由 ``start_bot`` / ``stop_bot`` 管。
    模板选项（网关 / 心跳 / 超时）构造时给定，Bot Token 按机器人各填各的。
    """

    def __init__(
        self,
        options: KookOptions | None = None,
        *,
        publish: EventSubscriber | None = None,
        logger: BaseLogger | None = None,
    ) -> None:
        """
        :param options: 网关地址 / 心跳 / 超时等（作为模板，Bot Token 按机器人另给）；
        :param publish: Gateway 的投递口（``gateway.publish``）：没给事件只记 debug 丢弃；
        :param logger: 业务日志实例，默认 ``bridge`` 那个。
        """
        self._publish: EventSubscriber | None = publish
        self._log: BaseLogger = logger if logger is not None else bridge_logger()
        self._template: KookOptions = options if options is not None else KookOptions()
        #: bot_id -> 客户端（一个 Bot Token 一个客户端）
        self._clients: dict[str, KookClient] = {}
        #: 已经 start 过的 bot_id（start 幂等靠它，避免同客户端重复起连接循环）
        self._started: set[str] = set()
        #: 机器人号 -> bot_id 反查，供 ``send`` 在「按 self_id 路由」时定位客户端
        self._by_self: dict[str, str] = {}
        #: bot_id -> 归属用户（凭证行 owner_id）：事件 / 在线列表的 owner 口径
        self._owners: dict[str, str] = {}
        #: 收尾标志（serve_forever 的退出条件）
        self._stopping: bool = False
        #: 停下来的通知事件（serve_forever 等它，不再轮询）
        self._stopped: asyncio.Event = asyncio.Event()

    #: 平台标识（BotAdapter 协议的路由键）
    platform: str = PLATFORM

    # ------------------------------------------------- 客户端登记与生命周期
    def has_bot(self, bot_id: str) -> bool:
        """这个机器人有没有客户端登记在册。"""
        return bot_id in self._clients

    def add_bot(self, bot_id: str, token: str, *, owner_id: str = "") -> None:
        """登记一个机器人（建 ``KookClient``），**不**连接；连接走 ``start_bot`` / ``start``。

        ``owner_id`` 是这个机器人属于哪个框架用户（凭证行 owner_id）；没凭证行的兼容路径
        传空串。幂等：已经登记过就什么都不做。
        """
        if bot_id in self._clients:
            return
        client = KookClient(
            _bot_options(self._template, token),
            handler=self._handler_for(bot_id),
            logger=self._log,
        )
        self._clients[bot_id] = client
        self._owners[bot_id] = owner_id

    async def start_bot(self, bot_id: str) -> None:
        """起一个机器人的连接（幂等：起过的跳过）。"""
        client = self._clients.get(bot_id)
        if client is None or bot_id in self._started:
            return
        self._started.add(bot_id)
        await client.start()

    async def stop_bot(self, bot_id: str) -> None:
        """停一个机器人的连接（登记还在，随时能 ``start_bot`` 回来）。"""
        client = self._clients.get(bot_id)
        self._started.discard(bot_id)
        if client is not None:
            await client.stop()

    async def remove_bot(self, bot_id: str) -> None:
        """停掉并注销一个机器人的客户端。"""
        client = self._clients.pop(bot_id, None)
        self._started.discard(bot_id)
        self._by_self = {self_id: bid for self_id, bid in self._by_self.items() if bid != bot_id}
        self._owners.pop(bot_id, None)
        if client is not None:
            await client.stop()

    # ------------------------------------------------------------------ 事件翻译
    def _handler_for(self, bot_id: str):
        """给某个机器人的客户端装事件钩子：闭包住 bot_id，事件能归对机器人。"""

        async def on_event(event: KookEvent) -> None:
            await self._on_event(bot_id, event)

        return on_event

    async def _on_event(self, bot_id: str, event: KookEvent) -> None:
        """客户端事件钩子：记下机器人号反查，翻译成规范化事件投给 Gateway。"""
        if event.self_id:
            self._by_self[event.self_id] = bot_id
        publish = self._publish
        if publish is None:
            self._log.debug(
                "事件没有投递口，已丢弃",
                platform=PLATFORM,
                type=event.type,
                target_id=event.target_id,
            )
            return
        await publish(_translate(event, owner_id=self._owners.get(bot_id, "")))

    # ------------------------------------------------------------------ BotAdapter 协议
    async def start(self) -> None:
        """起所有已登记机器人的连接（幂等）。"""
        self._stopping = False
        self._stopped.clear()
        for bot_id in tuple(self._clients):
            await self.start_bot(bot_id)

    async def stop(self) -> None:
        """停所有客户端：断开连接、停心跳（幂等）。"""
        self._stopping = True
        self._stopped.set()
        self._started.clear()
        for client in self._clients.values():
            await client.stop()

    async def serve_forever(self) -> None:
        """起连接并一直等到被停（bootstrap 主协程的退出条件之一）。"""
        await self.start()
        await self._stopped.wait()

    def clients(self, *, owner_id: str | None = None) -> tuple[BotClient, ...]:
        """在线列表快照：每个连上的机器人一行（归属 = 框架用户），没连上是空。

        「连没连」由客户端自己交代（``connected``），不靠 self_id —— 机器人刚连上、
        还没收到第一条事件（self_id 未学到）时，连接也是活的，该列出来。
        """
        rows: list[BotClient] = []
        for bot_id, client in self._clients.items():
            if not client.connected:
                continue
            if owner_id is not None and bot_id != owner_id:
                continue
            rows.append(
                BotClient(
                    client_id=client.self_id or bot_id,
                    owner_id=self._owners.get(bot_id, ""),
                    account="",
                    self_id=client.self_id,
                    remote="",
                    connected_at=client.connected_at,
                )
            )
        return tuple(rows)

    async def send(self, owner_id: str, action: str, /, **params: object) -> ActionResult:
        """给某个机器人发一个动作（走 REST），回执翻译成 ``ActionResult``。

        ``owner_id`` 优先按 bot_id 定位，退而按机器人号（self_id）定位；只有一个机器人时
        不纠结、直接交它（兼容单 bot 的旧用法）。

        :raises ConnectionError: 找不到对应机器人 / 网络失败（由 ``KookClient.call`` 抛）。
        """
        client = self._resolve_client(owner_id)
        if client is None:
            raise ConnectionError(
                f"[kook] 没有 owner={owner_id!r} 的机器人（已登记：{'、'.join(self._clients) or '无'}）"
            )
        response: KookActionResponse = await client.call(action, **params)
        return ActionResult(
            ok=response.ok,
            message="" if response.ok else response.message,
            data=response.data,
            raw=response,
        )

    async def reply(self, target: ChatTarget, content: str) -> ActionResult:
        """回复到 ``target`` 指向的会话：群聊回频道、私聊回 DM（id 是字符串，直接用）。

        ``target`` 是**本适配器**翻译事件时构造的 :class:`KookTarget`（生产与消费
        同平台，见 :class:`ChatTarget`），回复时原样传回即可；不是本平台的 target
        当场 ValueError（装配错位看得见）。动作契约与 OneBot 分开（``send_channel_msg`` /
        ``send_dm_msg``，参数键 ``target_id``）。
        """
        if not isinstance(target, KookTarget):
            raise ValueError(
                f"回复目标不是 Kook 的 target（{type(target).__name__}），"
                "生产与消费必须同平台"
            )
        if target.chat == "group":
            if not target.chat_id:
                raise ValueError("频道回复需要 chat_id（会话定位缺频道号）")
            return await self.send(
                target.owner_id, "send_channel_msg", target_id=target.chat_id, content=content
            )
        if target.chat == "private":
            if not target.user_id:
                raise ValueError("私聊回复需要 user_id（会话定位缺对方账号）")
            return await self.send(
                target.owner_id, "send_dm_msg", target_id=target.user_id, content=content
            )
        raise ValueError(f"会话定位的会话指向不明（chat={target.chat!r}），回不了")

    def make_target(
        self,
        *,
        owner_id: str,
        chat: str = "other",
        chat_id: str = "",
        user_id: str = "",
        message_id: str = "",
    ) -> ChatTarget:
        """从通用会话字段构造 Kook 的回程地址（id 是字符串，原样存）。

        画布 target 节点手动填的会话号本来就是字符串，直接进 target：群聊用 ``chat_id``
        当频道号、私聊用 ``user_id``（缺省回退 ``chat_id``）当对方账号。
        """
        return KookTarget(
            owner_id=owner_id,
            chat=chat,
            chat_id=chat_id if chat == "group" else "",
            user_id=user_id or chat_id if chat == "private" else "",
            message_id=message_id,
        )

    def _resolve_client(self, owner_id: str) -> KookClient | None:
        """按 bot_id -> 机器人号 -> 单实例兜底 的顺序找一个客户端。"""
        client = self._clients.get(owner_id)
        if client is not None:
            return client
        client = self._clients.get(self._by_self.get(owner_id, ""))
        if client is not None:
            return client
        if len(self._clients) == 1:
            return next(iter(self._clients.values()))
        return None