"""OneBot 反向 WS 服务端：框架监听端口，等 OneBot 实现（go-cqhttp / NapCat / LLOneBot …）连进来。

反向 WS 的收发规则（v11）::

    客户端 -> 框架：事件（带 post_type）；或某个动作的回应（带 status / retcode / echo）
    框架 -> 客户端：动作（{"action": ..., "params": {...}, "echo": ...}）

**一个端口接很多客户端**：谁连进来由**令牌**决定归属 —— 配了 :class:`~nacho.api.api.onebot.protocols.TokenRegistry` 时，
握手阶段把令牌翻成「谁的」（``id``）与机器人账号（``account``），两者绑在这条连接上
（:attr:`OneBotConnection.id` / :attr:`OneBotConnection.account`）。
没配注册表就不校验（谁都能连，归属记成匿名），所以不接数据库照样能跑起来。

在线的客户端像路由器的「已连接设备」那样列得出来（:meth:`OneBotServer.roster`），也能踢掉
（:meth:`OneBotServer.kick`）—— 但 OneBot 实现都会自动重连，要真删掉得连令牌一起吊销
（``kick(..., revoke=True)`` 或 :meth:`OneBotServer.revoke`）。

本模块只管「连接 + 协议」这一层：握手鉴权、连接管理、事件分发、动作发送；**业务不在这里** ——
要处理事件，建服务时传一个 ``handler``：``async def on_event(conn, event) -> None``。没传就只记日志。

用法::

    from nacho.platforms.onebot import OneBotOptions, OneBotServer

    server = OneBotServer(OneBotOptions(host="0.0.0.0", port=6700), handler=on_event)
    await server.start()
    await server.serve_forever()
"""
from __future__ import annotations

import asyncio
import json
import time
import weakref
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, TypeAlias, cast
from urllib.parse import unquote
from uuid import uuid4

from pydantic import ValidationError
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from nacho.core.logger import BaseLogger

from .logging import ONEBOT_LOGGER_NAME, onebot_logger
from .models import (
    ActionResponse,
    MetaEvent,
    OneBotEvent,
    is_event,
    parse_action_response,
    parse_event,
)
from .options import OneBotOptions

if TYPE_CHECKING:
    # 协议是接口层（nacho.api）与 onebot 共用的一份；运行时绝不 import nacho.api，
    # 否则 onebot 会被迫带上 api 的重型依赖（fastapi 等）。注解靠 PEP 563 惰性化。
    from nacho.api.api.onebot.protocols import TokenRegistry

#: 事件处理器：收到一条事件时的回调。抛出的异常只会被记下来，不影响后续事件。
EventHandler: TypeAlias = Callable[["OneBotConnection", OneBotEvent], Awaitable[None]]


@dataclass(frozen=True)
class ClientEntry:
    """在线列表里的一行（路由器「已连接设备」那样的一条）。

    注意这是**快照**：调用 :meth:`OneBotServer.roster` 那一刻的样子，不代表此刻还连着。
    """

    #: 这条连接自己的编号（踢人时按它定位）
    client_id: str
    #: 这条连接属于谁（由握手时的令牌定下来；语义本层不管）
    id: str
    #: 接入 WS 的那个 OneBot 机器人账号（令牌里带的那个）
    account: str
    #: 机器人号；还没收到事件时是 ``None``
    self_id: int | None
    #: 对端地址
    remote: str
    #: 连上的时刻（Unix 秒）
    connected_at: float
    #: 这个机器人那一行的主键（bot_id，多实例后与归属 owner_id 分开）；匿名是空串
    bot_id: str = ""


class OneBotConnection:
    """一条已连上的 OneBot 连接：发动作、等回应。

    连接上带几样身份信息，注意它们的**时点**不同：

    * ``id`` —— **谁的**（握手时由令牌定下来，本层不解释语义），比任何事件都早；
    * ``account`` —— 接入 WS 的那个 OneBot 机器人账号，同样握手时就有；
    * ``token`` —— 客户端带的明文令牌（吊销时用得着），也是握手时就有；
    * ``self_id`` —— 收到第一条事件后才学到（``None`` 表示还没收到）。
    """

    def __init__(
        self,
        ws: ServerConnection,
        *,
        options: OneBotOptions,
        logger: BaseLogger,
        id: str = "",
        bot_id: str = "",
        account: str = "",
        token: str = "",
    ) -> None:
        self._ws: ServerConnection = ws
        self._options: OneBotOptions = options
        self._log: BaseLogger = logger
        #: echo -> 等回应的 future；发动作时装上，收到回应 / 断开时摘掉
        self._pending: dict[str, asyncio.Future[ActionResponse]] = {}
        #: 这条连接自己的编号（在线列表列出来、踢人时按它定位）
        self.client_id: str = uuid4().hex[:12]
        #: 连上的时刻（Unix 秒）
        self.connected_at: float = time.time()
        #: 这条连接属于谁（没配令牌注册表时是空串 = 匿名）
        self.id: str = id
        #: 这个机器人那一行的主键（bot_id，多实例后与归属 owner_id 分开）；匿名是空串
        self.bot_id: str = bot_id
        #: 接入 WS 的那个 OneBot 机器人账号；没配注册表时是空串
        self.account: str = account
        #: 握手时带的明文令牌；没配注册表时是空串
        self.token: str = token
        #: 最近一次事件里的机器人号；一条连接通常就一个 bot
        self.self_id: int | None = None

    @property
    def remote(self) -> str:
        """对端地址（形如 ``127.0.0.1:53210``）。"""
        # websockets 把 remote_address 标成 Any（unix socket 时可能是别的形状），这里 cast 表态
        address = cast("tuple[str, int] | None", self._ws.remote_address)
        return f"{address[0]}:{address[1]}" if address is not None else "?"

    @property
    def path(self) -> str:
        """握手请求的路径（含查询串）。"""
        request = self._ws.request
        return request.path if request is not None else ""

    def entry(self) -> ClientEntry:
        """在线列表里的一行（快照）。"""
        return ClientEntry(
            client_id=self.client_id,
            id=self.id,
            bot_id=self.bot_id,
            account=self.account,
            self_id=self.self_id,
            remote=self.remote,
            connected_at=self.connected_at,
        )

    async def call(self, action: str, /, **params: object) -> ActionResponse:
        """发一个动作并等它的回应。

        ``echo`` 由本方法自动生成，用来把回应认回这一次调用；超过 ``action_timeout`` 还没
        回应就抛 :class:`TimeoutError`，连接中途断了则抛 :class:`ConnectionError`。
        """
        echo = uuid4().hex
        future: asyncio.Future[ActionResponse] = asyncio.get_running_loop().create_future()
        self._pending[echo] = future
        payload = json.dumps(
            {"action": action, "params": params, "echo": echo}, ensure_ascii=False
        )
        try:
            await self._ws.send(payload)
            return await asyncio.wait_for(future, self._options.action_timeout)
        finally:
            self._pending.pop(echo, None)

    async def close(self, *, code: int = 1000, reason: str = "") -> None:
        """主动断开这条连接。"""
        await self._ws.close(code, reason)

    # ------------------------------------------------- 供 OneBotServer 调用
    def accept(self, payload: Mapping[str, object]) -> OneBotEvent | None:
        """处理一条入站报文：动作回应就地交给等待方并返回 ``None``，事件解析后返回给服务端分发。

        :raises pydantic.ValidationError: 是事件但形状不对（缺字段 / ``post_type`` 不认）。
        """
        if not is_event(payload):
            response = parse_action_response(payload)
            if not self._resolve(response):
                self._log.warning(
                    "onebot 收到对不上的动作回应，已忽略",
                    owner_id=self.id,
                    echo=response.echo,
                    remote=self.remote,
                )
            return None
        return parse_event(payload)

    def fail_pending(self, exc: BaseException) -> None:
        """连接断了：把还在等回应的调用全部叫醒（抛 ``exc``），别让它们干等到超时。"""
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    # ------------------------------------------------------------------ 内部
    def _resolve(self, response: ActionResponse) -> bool:
        """把一条动作回应交回等它的调用；``echo`` 对不上返回 ``False``。"""
        future = self._pending.get(response.echo) if response.echo else None
        if future is None or future.done():
            return False
        future.set_result(response)
        return True


def _token_of(request: Request) -> str:
    """从握手请求里取令牌：``Authorization: Bearer <token>`` 或 ``?access_token=<token>``。

    取不到返回空串（由调用方按 401 处理）。
    """
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[7:].strip()
    query = request.path.split("?", 1)[1] if "?" in request.path else ""
    for item in query.split("&"):
        key, _, value = item.partition("=")
        if key == "access_token":
            return unquote(value)
    return ""


class OneBotServer:
    """反向 WS 服务端：监听端口、管理连接、把事件分发给 ``handler``。"""

    def __init__(
        self,
        options: OneBotOptions | None = None,
        *,
        handler: EventHandler | None = None,
        tokens: TokenRegistry | None = None,
        logger: BaseLogger | None = None,
    ) -> None:
        """
        :param options: 监听地址 / 路径 / 动作超时，默认全用 :class:`OneBotOptions` 的默认值；
        :param handler: 事件处理钩子，``async def on_event(conn, event) -> None``；不传就只记日志；
        :param tokens: 令牌注册表（令牌 -> 账号）；传了就**必须**带有效令牌才让连，
            不传则不校验（谁都能连，归属记成匿名）——不接数据库也能跑起来；
        :param logger: 业务日志实例，默认 ``onebot`` 那个。
        """
        self._options: OneBotOptions = options if options is not None else OneBotOptions()
        self._handler: EventHandler | None = handler
        self._tokens: TokenRegistry | None = tokens
        self._log: BaseLogger = (
            logger if logger is not None else onebot_logger(ONEBOT_LOGGER_NAME)
        )
        self._server: Server | None = None
        # 连着的客户端，三张 hash 索引（取谁都不遍历）：按连接编号取单条、按归属（谁的）分组、
        # 按机器人（bot_id）分组（「一个机器人一条连接」顶掉旧连接用）
        self._by_client_id: dict[str, OneBotConnection] = {}
        self._by_owner: dict[str, set[OneBotConnection]] = {}
        self._by_bot: dict[str, set[OneBotConnection]] = {}
        # 握手阶段查出的归属（connection -> (归属, bot_id, 机器人账号, 明文令牌)），_handle 里取走。
        # 用弱键字典：万一连接没走到 _handle 就被丢掉，条目会随对象回收自动消失，不会攒着
        self._greeted: weakref.WeakKeyDictionary[ServerConnection, tuple[str, str, str, str]] = (
            weakref.WeakKeyDictionary()
        )

    @property
    def options(self) -> OneBotOptions:
        """当前生效的选项。"""
        return self._options

    @property
    def tokens(self) -> TokenRegistry | None:
        """令牌注册表（没配就是 ``None`` = 不校验）。"""
        return self._tokens

    @property
    def connections(self) -> tuple[OneBotConnection, ...]:
        """当前连着的客户端（快照）。"""
        return tuple(self._by_client_id.values())

    # ------------------------------------------------------------------ 在线列表
    def roster(self, *, id: str | None = None) -> tuple[ClientEntry, ...]:
        """在线客户端列表（快照）：路由器「已连接设备」那一张表。

        :param id: 只看某个归属（谁的）下的客户端；``None`` 表示全部。
        """
        # 按归属取走 hash 索引那一格，不扫全集
        conns = self._by_client_id.values() if id is None else self._by_owner.get(id, set())
        return tuple(conn.entry() for conn in conns)

    async def kick(self, client_id: str, *, revoke: bool = False) -> bool:
        """把一个客户端踢下线；没有这条连接返回 ``False``。

        :param revoke: 连它的令牌一起吊销。OneBot 实现**都会自动重连**，
            只踢不断令牌的话过几秒它又会出现在列表里；要真删掉就用 ``revoke=True``。
        """
        conn = self._by_client_id.get(client_id)
        if conn is None:
            return False
        if revoke and conn.bot_id and self._tokens is not None:
            await self._tokens.remove_by_id(conn.bot_id)
        self._log.info(
            "onebot 客户端被踢下线",
            owner_id=conn.id,
            client=client_id,
            account=conn.account,
            revoked=revoke,
        )
        await conn.close(reason="kicked")
        return True

    async def revoke_by_id(self, token_id: str) -> bool:
        """吊销一个令牌（按记录 id），并把正用它连着的客户端全部断开。

        从列表里「删掉」一个客户端走的正是这条路径：只删令牌不断连接，它还挂在列表上；
        只断连接不删令牌，它过几秒就重连回来。

        返回令牌是不是真被删掉了（本来就不存在返回 ``False``）。
        """
        if self._tokens is None:
            return False
        removed = await self._tokens.remove_by_id(token_id)
        # 先快照再关：关连接会改动索引，边遍历边删不安全
        for conn in tuple(self._by_bot.get(token_id, set())):
            await conn.close(reason="token revoked")
        return removed

    async def set_token_enabled(self, token_id: str, enabled: bool) -> bool:
        """启用 / 停用一条令牌（记录还在，随时能启用回来；和"吊销"不同）。

        停用**不只是**「下次不许连」：正用它连着的客户端会一并断开——否则令牌明明
        被禁用了，客户端却还挂在在线列表上，看着像开关没生效。断开后它重连会被 401 拒。
        """
        if self._tokens is None:
            return False
        changed = await self._tokens.set_enabled(token_id, enabled)
        if changed and not enabled:
            for conn in tuple(self._by_bot.get(token_id, set())):
                await conn.close(reason="token disabled")
        return changed

    # ------------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        """开始监听（幂等：已经在监听就什么都不做）。"""
        if self._server is not None:
            return
        self._server = await serve(
            self._handle,
            self._options.host,
            self._options.port,
            process_request=self._process_request,
            # websockets 自己每条连接都记一条，太吵；连接事件我们自己记
            logger=None,
        )
        self._log.info(
            "onebot 反向 WS 已监听",
            host=self._options.host,
            port=self._options.port,
            path=self._options.path,
            auth=self._tokens is not None,
        )

    async def serve_forever(self) -> None:
        """起服务并一直等到被停（``stop`` 或进程被中断）。"""
        await self.start()
        assert self._server is not None
        await self._server.wait_closed()

    async def stop(self) -> None:
        """停服：关掉监听并断开所有客户端。"""
        server = self._server
        self._server = None
        if server is not None:
            server.close()  # 不再收新连接，并关掉现有连接（对端会收到 GOING_AWAY）
            await server.wait_closed()
            self._by_client_id.clear()
            self._by_owner.clear()
            self._by_bot.clear()
            self._log.info("onebot 反向 WS 已停止")

    # ------------------------------------------------------------------ 握手
    async def _process_request(
        self, connection: ServerConnection, request: Request
    ) -> Response | None:
        """握手时把门：路径不对 404、令牌认不出账号 401（返回 ``Response`` 即拒绝）。

        令牌要查注册表（可能是一次查库），所以这里是协程——``websockets`` 允许
        ``process_request`` 是协程函数。查出来的归属先暂存进 :attr:`_greeted`，紧接着的
        :meth:`_handle` 会取走（同一个 ``connection`` 对象）。
        """
        path = request.path.split("?", 1)[0]
        if self._options.path and path != self._options.path:
            self._log.warning(
                "onebot 握手路径不匹配，已拒绝", path=request.path, expect=self._options.path
            )
            return connection.respond(HTTPStatus.NOT_FOUND, "path not found\n")
        registry = self._tokens
        if registry is None:
            return None  # 没配注册表：不校验，归属记成匿名
        token = _token_of(request)
        record = await registry.resolve(token) if token else None
        if record is None:
            self._log.warning("onebot 握手令牌无效，已拒绝", path=request.path)
            return connection.respond(HTTPStatus.UNAUTHORIZED, "access token mismatch\n")
        # 归属（owner_id）与机器人主键（bot_id = record.id）分开：连接上绑归属、另记 bot_id
        self._greeted[connection] = (record.owner_id, record.id, record.account, token)
        return None

    # ------------------------------------------------------------------ 连接与分发
    async def _handle(self, ws: ServerConnection) -> None:
        """一条客户端连接的生命周期：登记 -> 收报文 -> 断开时清理。

        收报文和跑业务**分成两条腿**：本协程只管读、解析、把事件塞进 ``inbox``；另起一个
        worker（:meth:`_consume`）按序取出来交给 handler。这样 handler 里
        ``await conn.call(...)`` 等动作回应时，收报文这条腿还是活的 —— 否则回应读不进来，
        双方就死锁了。
        """
        # 握手阶段查出的归属（归属 / bot_id / 机器人账号 / 明文令牌），见 _process_request；没配时匿名
        owner_id, bot_id, account, token = self._greeted.pop(ws, ("", "", "", ""))
        # 一个机器人（bot_id）同时只允许一条连接：同 bot_id 已有连接时，新来的把旧的顶掉。
        # 只能对「有 bot_id」的连接生效——匿名模式下 bot_id 全是空串，互相顶会把客户端全踢光。
        # 用「顶掉」而不是「拒绝」：OneBot 实现断线都会自动重连，拒绝会让重连卡死；
        # 顶掉则始终保留最新一条连接，符合「一个机器人只能一条连接」的语义。
        if bot_id:
            for old in tuple(self._by_bot.get(bot_id, set())):
                # 先把旧连接从索引摘掉再关它：新连接登记后 roster 立刻只剩它一条
                self._by_client_id.pop(old.client_id, None)
                self._by_owner.get(old.id, set()).discard(old)
                self._by_bot[bot_id].discard(old)
                self._log.info(
                    "onebot 同机器人已有连接，新连接顶掉旧连接",
                    owner_id=owner_id,
                    bot_id=bot_id,
                    old_client=old.client_id,
                    old_remote=old.remote,
                )
                await old.close(reason="replaced by new connection")
        conn = OneBotConnection(
            ws,
            options=self._options,
            logger=self._log,
            id=owner_id,
            bot_id=bot_id,
            account=account,
            token=token,
        )
        self._by_client_id[conn.client_id] = conn
        self._by_owner.setdefault(conn.id, set()).add(conn)
        self._by_bot.setdefault(conn.bot_id, set()).add(conn)
        inbox: asyncio.Queue[OneBotEvent] = asyncio.Queue()
        worker = asyncio.create_task(
            self._consume(conn, inbox), name=f"onebot-events:{conn.remote}"
        )
        self._log.info(
            "onebot 客户端接入",
            owner_id=conn.id,
            remote=conn.remote,
            path=conn.path,
            account=conn.account,
            total=len(self._by_client_id),
        )
        try:
            async for raw in ws:
                await self._dispatch(conn, raw, inbox)
        except ConnectionClosed:
            pass  # 正常断开：对端关了，或我们主动关的
        except Exception:
            self._log.exception("onebot 连接处理异常", owner_id=conn.id, remote=conn.remote)
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
            self._by_client_id.pop(conn.client_id, None)
            owner_group = self._by_owner.get(conn.id)
            if owner_group is not None:
                owner_group.discard(conn)
                if not owner_group:  # 这个归属没连接了：空集合也摘掉，别攒着
                    del self._by_owner[conn.id]
            bot_group = self._by_bot.get(conn.bot_id)
            if bot_group is not None:
                bot_group.discard(conn)
                if not bot_group:  # 这个机器人没连接了：空集合也摘掉
                    del self._by_bot[conn.bot_id]
            conn.fail_pending(ConnectionError("连接已断开"))
            self._log.info(
                "onebot 客户端断开",
                owner_id=conn.id,
                remote=conn.remote,
                account=conn.account,
                total=len(self._by_client_id),
            )

    async def _consume(self, conn: OneBotConnection, inbox: asyncio.Queue[OneBotEvent]) -> None:
        """worker：按序把事件交给 handler（一条处理完再下一条，业务不用自己排队）。"""
        while True:
            event = await inbox.get()
            await self._emit(conn, event)

    async def _dispatch(
        self, conn: OneBotConnection, raw: str | bytes, inbox: asyncio.Queue[OneBotEvent]
    ) -> None:
        """一条原始报文：解 JSON -> 事件入队给 worker，动作回应就地交给等它的调用。"""
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        try:
            # json.loads 在 typeshed 里返回 Any，先 cast 成 object 表态，下面用 isinstance 现场校验
            loaded = cast(object, json.loads(text))
        except ValueError:
            self._log.warning(
                "onebot 收到非 JSON 文本，已忽略", owner_id=conn.id, remote=conn.remote
            )
            return
        if not isinstance(loaded, dict):
            self._log.warning(
                "onebot 收到非对象 JSON，已忽略", owner_id=conn.id, remote=conn.remote
            )
            return

        payload = cast("dict[str, object]", loaded)
        try:
            event = conn.accept(payload)
        except ValidationError as exc:
            self._log.warning(
                "onebot 报文解析失败，已忽略",
                owner_id=conn.id,
                remote=conn.remote,
                error=str(exc),
            )
            return
        if event is not None:
            # 机器人号要到第一条事件才露面：记到连接上，在线列表才列得出来
            if conn.self_id is None and event.self_id:
                conn.self_id = event.self_id
            inbox.put_nowait(event)

    async def _emit(self, conn: OneBotConnection, event: OneBotEvent) -> None:
        """把事件交给业务钩子。

        心跳（``meta_event`` / ``heartbeat``）只记日志、**不打扰业务**（几秒一条，交给 handler
        会逼着业务自己过滤）；其余事件（含生命周期）都照常交给 handler。
        """
        if isinstance(event, MetaEvent):
            if event.meta_event_type == "heartbeat":
                self._log.debug(
                    "onebot 心跳", owner_id=conn.id, remote=conn.remote, self_id=event.self_id
                )
                return
            self._log.info(
                "onebot 生命周期",
                owner_id=conn.id,
                remote=conn.remote,
                self_id=event.self_id,
                sub_type=event.sub_type,
            )
        if self._handler is None:
            self._log.debug(
                "onebot 未注册事件处理器，事件已丢弃",
                owner_id=conn.id,
                post_type=event.post_type,
                remote=conn.remote,
            )
            return
        try:
            await self._handler(conn, event)
        except Exception:
            self._log.exception(
                "onebot 事件处理器抛出异常",
                owner_id=conn.id,
                post_type=event.post_type,
                remote=conn.remote,
            )
