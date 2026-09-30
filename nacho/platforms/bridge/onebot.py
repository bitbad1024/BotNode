"""OneBot 适配器：把 :class:`~nacho.platforms.onebot.OneBotServer` 包成第一个 ``BotAdapter``。

本模块是 bridge 里**唯一**允许 import ``nacho.platforms.onebot`` 的地方（平台胶水就住这儿；
``nacho.platforms.bridge`` 的 ``__init__`` 不碰它，没装 ``nacho[onebot]`` 照样能用模型 /
协议 / 总线）。第二个平台（Kook，P4）照这个模块的样子再写一份即可。

分工（包一层，不改一层）——``nacho/platforms/onebot/`` 一行不动，这里做三件事：

* **事件翻译**：``OneBotEvent`` -> :class:`~nacho.platforms.bridge.models.PlatformEvent`（身份转
  字符串、会话指向归一），翻译完调 ``publish`` 投给 Gateway。心跳不用滤——服务端的
  ``_emit`` 在调 handler **之前**就把心跳滤掉了（口径沿用，不重复做）；
* **能力转述**：``clients()`` / ``send()`` 实现 ``BotAdapter`` 协议——在线列表翻成
  :class:`~nacho.platforms.bridge.models.BotClient`，发动作按 onebot 节点同一套挑连接规则；
* **兼容面**：roster / kick / revoke_by_id / set_token_enabled / tokens / connections
  原样透传给被包的服务端——接口层的 ``OneBotLike`` 协议与工作流的 ``ctx.onebot``
  鸭子形状（P2 验收线：``router.py`` 与 ``nodes/onebot.py`` 零改动）由本适配器
  **结构化满足**，P2-5 装配时把它注到原来的注入点上即可。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from nacho.core.logger import BaseLogger, get_logger
from nacho.platforms.onebot import OneBotOptions, OneBotServer
from nacho.platforms.onebot.models import (
    ActionResponse,
    MessageEvent,
    MetaEvent,
    NoticeEvent,
    OneBotEvent,
    RequestEvent,
)
from nacho.platforms.onebot.server import ClientEntry, OneBotConnection

from .gateway import EventSubscriber
from .models import ActionResult, BotClient, PlatformEvent

if TYPE_CHECKING:
    from nacho.api.api.onebot.protocols import TokenRegistry


#: 本适配器的平台标识（路由键；Gateway 里不得与其它适配器重复）
PLATFORM = "onebot"


def _translate(conn: OneBotConnection, event: OneBotEvent) -> PlatformEvent:
    """一条平台事件 -> 规范化事件（翻译不了的字段整条挂在 ``raw`` 上兜底）。"""
    self_id = str(event.self_id) if event.self_id else ""
    if isinstance(event, MessageEvent):
        chat = event.message_type  # "private" / "group"，恰好就是归一口径
        chat_id = str(event.group_id) if event.message_type == "group" else str(event.user_id)
        return PlatformEvent(
            platform=PLATFORM,
            owner_id=conn.id,
            self_id=self_id,
            kind="message",
            chat=chat,
            chat_id=chat_id,
            user_id=str(event.user_id),
            text=event.raw_message,
            message_id=str(event.message_id) if event.message_id is not None else "",
            time=float(event.time),
            raw=event,
        )
    if isinstance(event, NoticeEvent):
        # 通知：群通知带 group_id 算群事件，其余算不出会话指向
        has_group = event.group_id is not None
        # 撤回类通知（friend_recall / group_msg_recall）带 message_id：NoticeEvent 没声明
        # 这字段，但 extra="allow" 的额外字段可属性访问——取到就带上，下游不用下探 raw
        recall_id = getattr(event, "message_id", None)
        return PlatformEvent(
            platform=PLATFORM,
            owner_id=conn.id,
            self_id=self_id,
            kind="notice",
            chat="group" if has_group else "other",
            chat_id=str(event.group_id) if has_group else "",
            user_id=str(event.user_id) if event.user_id is not None else "",
            message_id=str(recall_id) if recall_id is not None else "",
            time=float(event.time),
            raw=event,
        )
    if isinstance(event, RequestEvent):
        has_group = event.group_id is not None
        return PlatformEvent(
            platform=PLATFORM,
            owner_id=conn.id,
            self_id=self_id,
            kind="request",
            chat="group" if has_group else "private",
            chat_id=str(event.group_id) if has_group else "",
            user_id=str(event.user_id) if event.user_id is not None else "",
            text=event.comment,
            time=float(event.time),
            raw=event,
        )
    if isinstance(event, MetaEvent):
        # 生命周期一类，没有会话指向；文本留空
        return PlatformEvent(
            platform=PLATFORM,
            owner_id=conn.id,
            self_id=self_id,
            kind="meta",
            time=float(event.time),
            raw=event,
        )
    # 四类穷举完还到不了这里：OneBotEvent 联合扩了新类别而翻译没跟上——宁可当场炸，
    # 也别静默归成 meta（python -O 下 assert 会被整条剥掉，靠不住）
    raise TypeError(f"未认识的 OneBot 事件类型：{type(event).__name__}")


def _client(entry: ClientEntry) -> BotClient:
    """在线列表一行 -> 规范化一行（身份统一字符串口径）。"""
    return BotClient(
        client_id=entry.client_id,
        owner_id=entry.id,
        account=entry.account,
        self_id="" if entry.self_id is None else str(entry.self_id),
        remote=entry.remote,
        connected_at=entry.connected_at,
    )


class OneBotAdapter:
    """OneBot 平台适配器：包一个 ``OneBotServer``，实现 ``BotAdapter`` + 兼容面。

    服务端是**自己建的**（构造时把 ``handler=self._on_event`` 装进去）：``OneBotServer``
    的事件钩子在构造时定死，事后没有换钩子的口子——与其让调用方先建好服务端再传进来
    （还得叮嘱它别配 handler），不如适配器连建带包一步到位，``.server`` 属性留给要下探
    平台细节的场合。
    """

    def __init__(
        self,
        options: OneBotOptions | None = None,
        *,
        publish: EventSubscriber | None = None,
        tokens: TokenRegistry | None = None,
        logger: BaseLogger | None = None,
    ) -> None:
        """
        :param options: 监听地址 / 路径 / 动作超时（原样交给 ``OneBotServer``）；
        :param publish: Gateway 的投递口（``gateway.publish``）：没给（或事后才建 Gateway）
            事件只记 debug 丢弃——不抛，适配器自己也能单测；
        :param tokens: 令牌注册表（原样交给 ``OneBotServer``，鉴权 / 归属都在那边）；
        :param logger: 业务日志实例，默认 ``bridge`` 那个。
        """
        self._publish: EventSubscriber | None = publish
        self._log: BaseLogger = logger if logger is not None else get_logger("bridge")
        self._server: OneBotServer = OneBotServer(
            options, handler=self._on_event, tokens=tokens
        )

    #: 平台标识（BotAdapter 协议的路由键）
    platform: str = PLATFORM

    @property
    def server(self) -> OneBotServer:
        """被包的 OneBot 服务端（下探平台细节用的口子；常规能力走本适配器）。"""
        return self._server

    # ------------------------------------------------------------------ 事件翻译
    async def _on_event(self, conn: OneBotConnection, event: OneBotEvent) -> None:
        """服务端事件钩子：翻译成规范化事件投给 Gateway。

        心跳到不了这里（服务端 ``_emit`` 已滤）；本方法抛不出的异常由服务端兜底记日志。
        """
        publish = self._publish
        if publish is None:
            self._log.debug(
                "事件没有投递口，已丢弃",
                platform=PLATFORM,
                owner_id=conn.id,
                post_type=event.post_type,
            )
            return
        await publish(_translate(conn, event))

    # ------------------------------------------------------------------ BotAdapter 协议
    async def start(self) -> None:
        """开始监听（幂等，语义对齐 ``OneBotServer.start``）。"""
        await self._server.start()

    async def stop(self) -> None:
        """停服：关监听并断开所有客户端（幂等）。"""
        await self._server.stop()

    def clients(self, *, owner_id: str | None = None) -> tuple[BotClient, ...]:
        """在线列表快照（规范化口径）；给 ``owner_id`` 就只看那个归属下的。"""
        return tuple(_client(entry) for entry in self._server.roster(id=owner_id))

    async def send(self, owner_id: str, action: str, /, **params: object) -> ActionResult:
        """给 ``owner_id`` 的在线连接发一个动作并等回执。

        挑连接的规则与 onebot 节点同一套：归属匹配 + 取最近连上的那条。参数原样转述
        （群号 / 用户号转整数是**调用方**的事，onebot 节点已经在做）。

        :raises ConnectionError: 这个归属下没有在线连接（环境问题当场抛）。
        """
        matches = [conn for conn in self._server.connections if conn.id == owner_id]
        if not matches:
            online = "、".join(
                sorted({conn.id or "(匿名)" for conn in self._server.connections})
            ) or "无"
            raise ConnectionError(
                f"[onebot] 没有归属 {owner_id or '(空)'} 的在线连接（当前在线：{online}）"
            )
        conn = max(matches, key=lambda item: item.connected_at)
        response: ActionResponse = await conn.call(action, **params)
        return ActionResult(
            ok=response.ok,
            message="" if response.ok else f"status={response.status} retcode={response.retcode}",
            data=response.data,
            raw=response,
        )

    # ------------------------------------------------------------------ 兼容面（透传）
    # 接口层 OneBotLike 协议 + 工作流 ctx.onebot 鸭子形状，P2 验收线：下游零改动。
    @property
    def tokens(self) -> TokenRegistry | None:
        """令牌注册表（没配就是 ``None`` = 不校验）。"""
        return self._server.tokens

    @property
    def connections(self) -> tuple[OneBotConnection, ...]:
        """当前连着的客户端（快照）；工作流 ``ctx.onebot`` 靠它挑连接。"""
        return self._server.connections

    def roster(self, *, id: str | None = None) -> tuple[ClientEntry, ...]:
        """在线客户端列表（快照，**平台口径**：``self_id`` 仍是 ``int | None``）。"""
        return self._server.roster(id=id)

    async def kick(self, client_id: str, *, revoke: bool = False) -> bool:
        """踢掉一个客户端；``revoke=True`` 连令牌一起吊销。"""
        return await self._server.kick(client_id, revoke=revoke)

    async def revoke_by_id(self, token_id: str) -> bool:
        """吊销一个令牌（按记录 id），并把正用它连着的客户端断开。"""
        return await self._server.revoke_by_id(token_id)

    async def set_token_enabled(self, token_id: str, enabled: bool) -> bool:
        """启用 / 停用一条令牌；停用会连同断开正用它连着的客户端。"""
        return await self._server.set_token_enabled(token_id, enabled)

    async def serve_forever(self) -> None:
        """起服务并一直等到被停（bootstrap 的主协程停在这；对齐 ``OneBotServer``）。"""
        await self._server.serve_forever()
