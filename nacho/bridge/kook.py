"""Kook 适配器：把 :class:`~nacho.kook.KookClient` 包成第二个 ``BotAdapter``。

这是 bridge 里**第二个**允许 import 平台包的地方（第一个是 :mod:`nacho.bridge.onebot`），
用来验证 P2 定的 ``BotAdapter`` 协议是不是「真通用」——OneBot 是反向 WS（框架当服务端），
Kook 是正向 WS（框架当客户端），方向相反，但两边都要能塞进同一个 :class:`Gateway`。

与 OneBot 适配器的关键差异：

* **归属语义**：OneBot 一个端口接很多客户端、靠令牌分归属；Kook 一个 Bot Token 就是一个
  机器人，没有「多归属」概念。所以 ``owner_id`` 这里就是**机器人自身**（从事件里学到的
  ``self_id``，没学到是空串），发送时 ``send(owner_id, ...)`` 的 ``owner_id`` 其实不参与
  路由（单 bot），直接转述给 ``KookClient.call``；
* **事件翻译**：Kook 的 ``channel_type``（GROUP=频道 / PERSON=私聊）归一成 ``PlatformEvent``
  的 ``chat``，``target_id`` -> ``chat_id``，``author_id`` -> ``user_id``，``content`` ->
  ``text``，身份全是字符串（与 OneBot 的整数不同，但规范化后字符串口径正好统一）；
* **能力转述**：``clients()`` 只有一个「在线」行（连上了就有，没连上就是空），``send``
  走 REST（:meth:`KookClient.call`），回执翻译成 :class:`~nacho.bridge.models.ActionResult`。

不 import ``nacho.onebot`` / ``nacho.api`` / ``nacho.workflow`` —— 只依赖 ``nacho.core``
（logger）与 ``nacho.kook`` 平台包。
"""
from __future__ import annotations

from typing import Any

from nacho.core.logger import BaseLogger, get_logger
from nacho.kook import KookActionResponse, KookClient, KookEvent, KookOptions

from .gateway import EventSubscriber
from .models import ActionResult, BotClient, PlatformEvent

#: 本适配器的平台标识（路由键；Gateway 里不得与其它适配器重复）
PLATFORM = "kook"


def _translate(event: KookEvent) -> PlatformEvent:
    """一条 Kook 事件 -> 规范化事件（身份转字符串，翻译不了的字段挂 ``raw`` 兜底）。"""
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
        owner_id=event.self_id or "",  # 归属 = 机器人自身（Kook 单 bot）
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


def _client(entry: Any) -> BotClient:
    """在线列表一行 -> 规范化一行（Kook 单 bot，就一行）。"""
    return BotClient(
        client_id=entry.get("client_id", ""),
        owner_id=entry.get("owner_id", ""),
        account="",
        self_id=entry.get("self_id", ""),
        remote=entry.get("remote", ""),
        connected_at=entry.get("connected_at", 0.0),
    )


class KookAdapter:
    """Kook 平台适配器：包一个 ``KookClient``，实现 ``BotAdapter`` 协议。

    与 :class:`~nacho.bridge.onebot.OneBotAdapter` 的构造约定一致：客户端是**自己建的**
    （构造时把 ``handler=self._on_event`` 装进去），``.client`` 属性留给要下探平台细节的场合。
    """

    def __init__(
        self,
        options: KookOptions | None = None,
        *,
        publish: EventSubscriber | None = None,
        logger: BaseLogger | None = None,
    ) -> None:
        """
        :param options: 网关地址 / Bot Token / 心跳等（原样交给 ``KookClient``）；
        :param publish: Gateway 的投递口（``gateway.publish``）：没给事件只记 debug 丢弃；
        :param logger: 业务日志实例，默认 ``bridge`` 那个。
        """
        self._publish: EventSubscriber | None = publish
        self._log: BaseLogger = logger if logger is not None else get_logger("bridge")
        self._client: KookClient = KookClient(
            options, handler=self._on_event, logger=self._log
        )

    #: 平台标识（BotAdapter 协议的路由键）
    platform: str = PLATFORM

    @property
    def client(self) -> KookClient:
        """被包的 Kook 客户端（下探平台细节用的口子；常规能力走本适配器）。"""
        return self._client

    # ------------------------------------------------------------------ 事件翻译
    async def _on_event(self, event: KookEvent) -> None:
        """客户端事件钩子：翻译成规范化事件投给 Gateway。"""
        publish = self._publish
        if publish is None:
            self._log.debug(
                "事件没有投递口，已丢弃",
                platform=PLATFORM,
                type=event.type,
                target_id=event.target_id,
            )
            return
        await publish(_translate(event))

    # ------------------------------------------------------------------ BotAdapter 协议
    async def start(self) -> None:
        """开始连接（幂等，语义对齐 ``KookClient.start``）。"""
        await self._client.start()

    async def stop(self) -> None:
        """停客户端：断开连接、停心跳（幂等）。"""
        await self._client.stop()

    async def serve_forever(self) -> None:
        """起连接并一直等到被停（bootstrap 主协程的退出条件之一）。"""
        await self._client.serve_forever()

    def clients(self, *, owner_id: str | None = None) -> tuple[BotClient, ...]:
        """在线列表快照：连上了就一行（机器人自身），没连上是空。"""
        self_id = self._client.self_id
        if not self_id and owner_id is None:
            # 还没连上 / 还没学到 self_id：在线列表是空
            if not self._connected():
                return ()
        row = {
            "client_id": self_id or "kook",
            "owner_id": self_id or "",
            "self_id": self_id,
            "remote": "",
            "connected_at": 0.0,
        }
        if owner_id is not None and row["owner_id"] != owner_id:
            return ()
        return (_client(row),)

    def _connected(self) -> bool:
        """有没有连着（客户端内部是否持有连接）。"""
        return self._client._ws is not None  # noqa: SLF001 — 适配器与客户端同一层，可下探

    async def send(self, owner_id: str, action: str, /, **params: object) -> ActionResult:
        """给机器人发一个动作（走 REST），回执翻译成 ``ActionResult``。

        Kook 单 bot，``owner_id`` 不参与路由；参数原样转述给 ``KookClient.call``。

        :raises ConnectionError: 没配 Token / 网络失败（由 ``KookClient.call`` 抛）。
        """
        response: KookActionResponse = await self._client.call(action, **params)
        return ActionResult(
            ok=response.ok,
            message="" if response.ok else response.message,
            data=response.data,
            raw=response,
        )
