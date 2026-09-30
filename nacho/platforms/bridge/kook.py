"""Kook 适配器：把 :class:`~nacho.platforms.kook.KookClient` 包成第二个 ``BotAdapter``（**多客户端**）。

这是 bridge 里**第二个**允许 import 平台包的地方（第一个是 :mod:`nacho.platforms.bridge.onebot`），
用来验证 P2 定的 ``BotAdapter`` 协议是不是「真通用」——OneBot 是反向 WS（框架当服务端），
Kook 是正向 WS（框架当客户端），方向相反，但两边都要能塞进同一个 :class:`Gateway`。

与 OneBot 适配器的关键差异：

* **归属语义**：OneBot 一个端口接很多客户端、靠令牌分归属；Kook 一个 Bot Token 就是一个
  机器人，没有「多归属」概念。一个适配器管**多个**机器人（``dict[bot_id, KookClient]``，
  一个 Bot Token 一个客户端）——这样 Gateway 里 Kook 仍只占一个 platform 槽位，运行时
  通过 API 增删 Kook 机器人也不会撞上「同平台重复注册」的限制；
* **事件翻译**：Kook 的 ``channel_type``（GROUP=频道 / PERSON=私聊）归一成 ``PlatformEvent``
  的 ``chat``，``target_id`` -> ``chat_id``，``author_id`` -> ``user_id``，``content`` ->
  ``text``，身份全是字符串（与 OneBot 的整数不同，但规范化后字符串口径正好统一）；
* **能力转述**：``clients()`` 列出各在线机器人（一行一个），``send`` 走 REST
  （:meth:`KookClient.call`），回执翻译成 :class:`~nacho.platforms.bridge.models.ActionResult`。

不 import ``nacho.platforms.onebot`` / ``nacho.api`` / ``nacho.workflow`` —— 只依赖
``nacho.core``（logger）与 ``nacho.platforms.kook`` 平台包。
"""
from __future__ import annotations

import asyncio

from nacho.core.logger import BaseLogger
from nacho.platforms.kook import KookActionResponse, KookClient, KookEvent, KookOptions

from .gateway import EventSubscriber
from .logging import bridge_logger
from .models import ActionResult, BotClient, PlatformEvent

#: 本适配器的平台标识（路由键；Gateway 里不得与其它适配器重复）
PLATFORM = "kook"


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
    )


def _bot_options(template: KookOptions, token: str) -> KookOptions:
    """按模板 + Bot Token 拼一份客户端选项：网关 / 心跳 / 超时沿用模板，凭证用各自的 Token。"""
    return KookOptions(
        gateway=template.gateway,
        token=token,
        secret_key="",
        heartbeat_interval=template.heartbeat_interval,
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