"""Kook 正向 WebSocket 客户端：连 Kook 网关、收事件、发动作。

与 OneBot 相反的方向：OneBot 是**反向 WS**（框架当服务端），这里是**正向 WS**（框架当
客户端，主动连 Kook 网关，用 Bot Token 鉴权）。所以本模块的核心是「建连 + 心跳保活 +
收事件分发 + 断线重连」，而不是「监听端口等实现连进来」。

收发规则（Kook 网关）::

    框架 -> 网关：signal 2（ping，带最近 sn）；或 REST API（发消息等动作）
    网关 -> 框架：signal 1（hello）/ signal 0（事件）/ signal 3（pong）/
                  signal 5（reconnect：这条连接已失效，要主动断开并重新获取网关）/
                  signal 6（resume ack：续传成功，带这一轮有效的 session_id）

两条超时按官方口径：连上后 6 秒内要收到 hello、发出 ping 后 6 秒内要收到 pong，
超了就主动断开重连（半开连接只能这样发现）。

Kook 的正向 WS **只推事件，不能发消息**；发消息走 **REST API**（HTTP POST，带 Bot Token）。
所以 :meth:`KookClient.call` 走 HTTP，而不是 WS —— 这是与 OneBot 的另一个关键差异。

本模块只管「连接 + 协议」这一层：建连鉴权、心跳保活、事件分发（``handler`` 钩子）、
动作发送、断线重连；**业务不在这里**。事件进 ``handler``：``async def on_event(event) -> None``。

用法::

    from botnode.platforms.kook import KookOptions, KookClient

    async def on_event(event):
        print(event.type, event.target_id, event.content)

    client = KookClient(KookOptions(token="xxx"), handler=on_event)
    await client.start()
    await client.serve_forever()
"""
from __future__ import annotations

import asyncio
import http.client
import json
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from typing import TypeAlias, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import ValidationError
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from botnode.core.logger import BaseLogger

from .logging import KOOK_LOGGER_NAME, kook_logger
from .models import KookActionResponse, KookEvent, parse_action_response, parse_event
from .options import KookOptions

#: 事件处理器：收到一条事件时的回调。抛出的异常只会被记下来，不影响后续事件。
EventHandler: TypeAlias = Callable[[KookEvent], Awaitable[None]]

#: REST API 基地址（发消息等动作走这里，不是 WS）
_DEFAULT_API: str = "https://www.kookapp.cn/api/v3"
#: 网关分发接口：Kook 的网关地址是动态下发的，连接前要 GET 这里拿真实 wss 地址
_GATEWAY_INDEX: str = f"{_DEFAULT_API}/gateway/index"


def _api_url(options: KookOptions, path: str) -> str:
    """拼 REST 端点的完整地址。"""
    return f"{_DEFAULT_API}{path}"


#: REST API 的 host（从基地址拆出来；连接复用按 host 建持久连接）
_DEFAULT_API_HOST: str = cast(str, urlsplit(_DEFAULT_API).hostname)
#: REST 瞬时失败值得重试的状态码：429（限流）与常见 5xx（服务端抖动）
_REST_RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})

#: 心跳超时（秒）：官方——发出 ping 后 6 秒内没收到 pong 就进入超时状态
_PONG_TIMEOUT: float = 6.0
#: 握手超时（秒）：官方——连上 websocket 后 6 秒内应收到 hello，否则算连接超时
_HELLO_TIMEOUT: float = 6.0
#: 心跳超时后的探活间隔（秒）：官方——先补发两次 ping（间隔 2、4），判断连接是否还活着
_PROBE_GAPS: tuple[float, float] = (2.0, 4.0)


class _RestRetryable(Exception):
    """REST 瞬时失败（429 / 5xx / 连接错误）：值得按退避重试。"""


def _jitter() -> float:
    """重连 / 重试退避的抖动系数：[0.8, 1.2)，避免多个客户端同时重试（thundering herd）。"""
    return random.uniform(0.8, 1.2)


#: 官方连接流程里「重连 + resume」一共就这四拍（2、4 / 8、16）；用完就回到第 1 步
_RESUME_STEPS: int = 4


def _reconnect_delay(options: KookOptions, attempts: int) -> float:
    """第 ``attempts`` 次重连前的等待：基准 × 2^(attempts-1)，封顶 + 抖动。

    官方连接流程：先两次重连（2、4 秒）、再两次 resume（8、16 秒，地址上带 resume 参数）；
    四拍用尽回到第 1 步「重新获取 Gateway」无限重试（指数退避，最大间隔 60 秒）。序列是
    连续的，所以这里就是一条 ``基准 × 2^k`` 的曲线，哪几拍属于哪个阶段由
    :meth:`KookClient._run_loop` 决定。
    """
    exponent = max(0, attempts - 1)
    base = min(options.reconnect_interval * (2 ** exponent), options.reconnect_max_interval)
    return base * _jitter()


def _with_resume(url: str, sn: int, session_id: str) -> str:
    """把断线重连的续传参数（``resume=1&sn=&session_id=``）拼进网关地址。

    Kook 网关协议：WebSocket 断开重连时，在 url 后追加 ``resume=1``、本地已处理的最大
    ``sn`` 与上一连接的 ``session_id``，服务端从该 sn 之后续传离线消息；没收到过任何
    事件（sn=0）且没有 session_id 就原样返回（全新连接，不需要续传）。
    """
    if sn <= 0 and not session_id:
        return url
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["resume"] = "1"
    if sn > 0:
        query["sn"] = str(sn)
    if session_id:
        query["session_id"] = session_id
    return urlunsplit(parts._replace(query=urlencode(query)))


def _rest_backoff(attempt: int) -> float:
    """REST 第 ``attempt`` 次重试前的等待：0.2s 指数退避封顶 1s，带抖动。"""
    return min(0.2 * (2 ** attempt), 1.0) * _jitter()


class KookClient:
    """Kook 正向 WS 客户端：连网关、收事件、发动作（REST）。

    :param options: 网关地址 / Bot Token / 心跳 / 超时（见 :class:`KookOptions`）；
    :param handler: 事件处理钩子，``async def on_event(event) -> None``；不传就只记日志；
    :param logger: 业务日志实例，默认 ``kook`` 那个。
    """

    def __init__(
        self,
        options: KookOptions | None = None,
        *,
        handler: EventHandler | None = None,
        logger: BaseLogger | None = None,
    ) -> None:
        self._options: KookOptions = options if options is not None else KookOptions()
        self._handler: EventHandler | None = handler
        self._log: BaseLogger = (
            logger if logger is not None else kook_logger(KOOK_LOGGER_NAME)
        )
        self._ws: object | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._hello_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        #: 事件收报 -> handler 之间的有界队列：收报文这条腿只往里面塞，另起 worker 按序取，
        #: 保证「一条处理完再下一条」并给上游背压（对齐 OneBot 的 _consume 语义）
        self._inbox: asyncio.Queue[KookEvent] = asyncio.Queue()
        #: 按序消费 inbox 的 worker；连上时起、断开时收
        self._consume_task: asyncio.Task[None] | None = None
        #: 收到 pong 的通知（心跳等它；等不到 = 超时）
        self._pong: asyncio.Event = asyncio.Event()
        self._stopping: bool = False
        #: 停下来的通知事件（serve_forever / _wait_until_stopped 等它，不再轮询）
        self._stopped: asyncio.Event = asyncio.Event()
        #: 最近收到的最大事件 sn：心跳 ping 带上它（Kook 靠它确认送达），重连 resume 也要
        self._sn: int = 0
        #: hello 下发的 session_id：断线重连 resume 时拼回网关地址
        self._session_id: str = ""
        #: 正在用的网关地址（不含 resume 参数）：续传必须复用同一个地址——每次都重新
        #: discover 会拿到带新 token 的地址，token 和 session 对不上，续传必被网关拒绝
        self._gateway_url: str = ""
        #: 本次连接有没有完成握手（收到 code==0 的 hello）：没握手就断开 = 续传被拒 /
        #: 网关直接掐线，收尾时要清掉续传状态，让下轮重连走全新连接
        self._greeted: bool = False
        #: 连上后从事件里学到的机器人自身 id；没学到是空串
        self.self_id: str = ""
        #: 最近一次连上的时刻（Unix 秒）；没连过是 0
        self.connected_at: float = 0.0
        #: REST 限流 + 连接复用的锁（同一时刻只发一个请求，持久连接才安全）
        self._rest_lock: asyncio.Lock = asyncio.Lock()
        #: 上一次 REST 请求完成的时刻（monotonic 秒），用于最小间隔限流
        self._last_rest: float = 0.0
        #: 懒建的持久 HTTP(S) 连接（同一 host 复用；瞬时失败即丢）
        self._rest_conn: http.client.HTTPConnection | None = None

    @property
    def options(self) -> KookOptions:
        """当前生效的选项。"""
        return self._options

    @property
    def connected(self) -> bool:
        """当前有没有连着网关（持有连接）。"""
        return self._ws is not None

    # ------------------------------------------------------------------ 连接
    async def start(self) -> None:
        """开始连接（幂等：已经连着就什么都不做）。"""
        if self._ws is not None:
            return
        self._stopping = False
        self._stopped.clear()
        self._reconnect_task = asyncio.create_task(self._run_loop(), name="kook-connect")

    async def serve_forever(self) -> None:
        """起连接并一直等到被停（``stop`` 或进程被中断）。"""
        await self.start()
        await self._wait_until_stopped()

    async def _wait_until_stopped(self) -> None:
        """挂起直到 ``stop()`` 被调用；用于 serve_forever 的退出条件。"""
        await self._stopped.wait()

    async def stop(self) -> None:
        """停客户端：关连接、停心跳、停重连（幂等）。"""
        self._stopping = True
        self._stopped.set()
        if self._hello_task is not None:
            self._hello_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._hello_task
            self._hello_task = None
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._heartbeat_task
            self._heartbeat_task = None
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reconnect_task
            self._reconnect_task = None
        ws = self._ws
        self._ws = None
        if ws is not None:
            await cast(object, ws).close()  # type: ignore[attr-defined]
            self._log.info("kook 客户端已停止")
        if self._rest_conn is not None:
            with suppress(Exception):
                self._rest_conn.close()
            self._rest_conn = None

    async def _close_current(self) -> None:
        """主动关掉当前连接：让收报文循环退出，由 :meth:`_run_loop` 重新连接。

        网关要求重连（signal 5）/ 握手失败 / 心跳超时都走这里 —— 官方口径是「客户端主动
        断开」，而不是等连接自己烂掉。
        """
        ws = self._ws
        if ws is None:
            return
        with suppress(Exception):
            await cast(object, ws).close()  # type: ignore[attr-defined]

    def _drain_inbox(self) -> None:
        """清空待处理事件队列：官方要求 reconnect 时连消息队列一起清掉，否则消息会错乱。"""
        while not self._inbox.empty():
            with suppress(asyncio.QueueEmpty):
                self._inbox.get_nowait()

    # ------------------------------------------------------------------ 收发循环
    async def _run_loop(self) -> None:
        """连接 -> 心跳 -> 收报文；断开按官方流程分阶段重连，直到 ``stop``。

        官方连接流程：连接失败先退避两次（2、4 秒）-> 再两次 resume（8、16 秒，地址带
        resume 参数）-> 都不行就回到第 1 步「重新获取 Gateway」无限重试（指数退避，上限
        60 秒）；握手没成功（hello 失败 / 超时）同样回退到第 1 步。
        """
        failures = 0
        while not self._stopping:
            try:
                greeted = await self._connect_once()
                if greeted:
                    failures = 0  # 跑完一整轮（握手成功 + 收过报文）：重新计数
                else:
                    failures += 1  # 握手没成：官方——回退到第 1 步
            except Exception as exc:  # noqa: BLE001 — 连不上就退出来等下轮重连
                failures += 1
                self._log.warning("kook 连接失败，稍后重连", error=str(exc))
            if self._stopping:
                return
            if failures > _RESUME_STEPS:
                # 四拍用尽：回到第 1 步，下次重新 discover（拿失效地址续传只会被拒）
                self._gateway_url = ""
            await asyncio.sleep(_reconnect_delay(self._options, failures))

    # ------------------------------------------------- REST（限流 + 重试 + 连接复用）
    async def _rest(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None = None,
    ) -> Mapping[str, object]:
        """带限流 + 重试的 REST 调用；动作与 gateway/index 共用这一条路。

        限流：两次请求至少隔 ``rest_min_interval`` 秒（``asyncio.Lock`` 串行化，顺带让
        持久连接同一时刻只被一个请求占用）；瞬时失败（429 / 5xx / 连接错误）按指数退避
        重试，最多 ``rest_max_retries`` 次，超了按环境问题抛 :class:`ConnectionError`。
        """
        async with self._rest_lock:
            wait = self._last_rest + self._options.rest_min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            last: Exception | None = None
            for attempt in range(self._options.rest_max_retries + 1):
                try:
                    payload = await asyncio.to_thread(
                        self._rest_once, method, url, headers=headers, body=body
                    )
                    self._last_rest = time.monotonic()
                    return payload
                except _RestRetryable as exc:
                    last = exc
                    if attempt >= self._options.rest_max_retries:
                        break
                    self._log.warning(
                        "kook REST 瞬时失败，准备重试",
                        method=method,
                        attempt=attempt + 1,
                        reason=str(exc),
                    )
                    await asyncio.sleep(_rest_backoff(attempt))
                except Exception as exc:  # noqa: BLE001 — 网络 / HTTP 错误统一按环境问题抛
                    self._last_rest = time.monotonic()
                    raise ConnectionError(f"REST 请求失败：{exc}") from exc
            self._last_rest = time.monotonic()
            raise ConnectionError(
                f"REST 多次失败（{self._options.rest_max_retries + 1} 次）：{last}"
            ) from last

    def _rest_once(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None,
    ) -> Mapping[str, object]:
        """发一次 HTTP 请求并解析 JSON 回应（同步，跑在 ``to_thread`` 里）。

        连接复用：同一 host 懒建一条持久连接（``self._rest_conn``），成功读完回应后留着
        下一条复用；空闲超过 ``rest_idle_timeout`` 就主动重建（服务端会按空闲时间掐连接，
        等到请求时才撞上已关的连接就得白试一次）。429 / 5xx / 连接错误都算瞬时失败，
        丢连接交给 :meth:`_rest` 重试。
        """
        parts = urlsplit(url)
        conn = self._rest_conn
        if (
            conn is not None
            and time.monotonic() - self._last_rest > self._options.rest_idle_timeout
        ):
            # 连接空闲太久：服务端（或中间链路）多半已经把它掐了 —— 主动重建，别等到发
            # 请求才撞上 "Remote end closed connection without response" 再白试一次
            with suppress(Exception):
                conn.close()
            conn = None
            self._rest_conn = None
        if conn is None:
            conn = http.client.HTTPSConnection(
                parts.hostname or _DEFAULT_API_HOST,
                timeout=self._options.action_timeout,
            )
            self._rest_conn = conn
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        try:
            conn.request(method, target, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            status = resp.status
            resp.close()
        except (OSError, http.client.HTTPException) as exc:
            self._rest_conn = None
            raise _RestRetryable(f"连接错误：{exc}") from exc
        if status in _REST_RETRYABLE_STATUS or status >= 500:
            self._rest_conn = None
            raise _RestRetryable(f"HTTP {status}")
        if status < 200 or status >= 300:
            self._rest_conn = None
            raise ConnectionError(f"REST 返回 HTTP {status}")
        return cast("Mapping[str, object]", json.loads(raw.decode("utf-8")))

    async def _discover_gateway(self) -> str:
        """调 gateway/index 拿真实网关地址（Kook 网关是动态下发的，不能硬编码）。

        网关地址通过 ``GET /api/v3/gateway/index`` 下发（带 Bot Token 鉴权），返回的
        ``data.url`` 里已经带好 token / compress 参数，直接拿它连即可。

        :raises ConnectionError: 没配 Bot Token / 网络失败 / 返回里没有 url。
        """
        token = self._options.token
        if not token:
            raise ConnectionError("[kook] 未配置 Bot Token，无法获取网关地址")
        try:
            payload = await self._rest(
                "GET",
                f"{_GATEWAY_INDEX}?compress=0",
                headers={"Authorization": f"Bot {token}"},
            )
        except ConnectionError as exc:
            raise ConnectionError(f"[kook] 获取网关地址失败：{exc}") from exc
        data = payload.get("data")
        url = data.get("url") if isinstance(data, dict) else None
        if not isinstance(url, str) or not url:
            raise ConnectionError("[kook] gateway/index 没返回网关地址")
        return url

    async def _connect_once(self) -> bool:
        """建一条连接并跑它的收报文循环；返回**握手有没有成功**（由 _run_loop 决定重连）。"""
        token = self._options.token
        # 网关地址只取一次、断线复用：续传必须用同一地址（同 token 才配得上 session_id）；
        # 每次都重新 discover 会拿新 token，resume 必被拒，形成「连上就断、断了又连」的死循环
        if not self._gateway_url:
            self._gateway_url = self._options.gateway or await self._discover_gateway()
        # 断线重连：把已处理的最大 sn 和上一连接的 session_id 拼进去，让网关续传而不是重放
        gateway = _with_resume(self._gateway_url, self._sn, self._session_id)
        self._greeted = False
        self._log.info("kook 正在连接网关", gateway=gateway, token_set=bool(token))
        async with ws_connect(
            gateway,
            additional_headers={"Authorization": f"Bot {token}"} if token else None,
            logger=None,
        ) as ws:
            self._ws = ws
            self.connected_at = time.time()
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat(), name="kook-heartbeat"
            )
            self._hello_task = asyncio.create_task(
                self._hello_watchdog(), name="kook-hello"
            )
            self._consume_task = asyncio.create_task(
                self._consume(), name="kook-events"
            )
            self._log.info("kook 已连接网关", gateway=gateway)
            try:
                async for raw in ws:
                    await self._handle_raw(cast("str | bytes", raw))
            finally:
                if self._hello_task is not None:
                    self._hello_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await self._hello_task
                    self._hello_task = None
                if self._heartbeat_task is not None:
                    self._heartbeat_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await self._heartbeat_task
                    self._heartbeat_task = None
                if self._consume_task is not None:
                    self._consume_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await self._consume_task
                    self._consume_task = None
                self._ws = None
        # 连接收尾：没完成握手就断了（resume 被拒 / 网关直接掐线）——清掉续传状态，
        # 让下轮重连走全新连接；否则会拿失效的 session_id 无限重试
        if not self._greeted:
            self._log.warning("kook 网关握手未完成（续传被拒或连接被掐），下次全新连接")
            self._session_id = ""
            self._sn = 0
            self._gateway_url = ""
        return self._greeted

    async def _hello_watchdog(self) -> None:
        """握手表：官方——连上 websocket 后 6 秒内应收到 hello，没收到就是连接超时。

        网关「连上了但不发 hello 也不掐线」这种半开连接，只能靠这一条发现。
        """
        await asyncio.sleep(_HELLO_TIMEOUT)
        if self._greeted:
            return
        self._log.warning("kook 握手超时，主动断开重连", timeout=_HELLO_TIMEOUT)
        await self._close_current()

    def _heartbeat_interval(self) -> float:
        """下一轮心跳的间隔：官方是 30 秒 + rand(-5, +5) —— 别让所有客户端同一时刻打心跳。

        抖动幅度取 ``min(heartbeat_jitter, interval / 2)``：配了很短的间隔（测试 / 特殊
        场合）时不至于把间隔抖成负数或凭空放大。
        """
        interval = self._options.heartbeat_interval
        jitter = min(self._options.heartbeat_jitter, interval / 2)
        return max(0.01, interval + random.uniform(-jitter, jitter))

    async def _probe_connection(self) -> bool:
        """心跳超时后的探活：官方——再补发两次 ping（间隔 2、4），收到 pong 就算连接还在。

        网络抖一下就可能丢一轮 pong，直接断连太激进；补两次还不回才算这条连接真死了。
        """
        for gap in _PROBE_GAPS:
            ws = self._ws
            if ws is None:
                return False
            self._pong.clear()
            with suppress(Exception):
                await cast(object, ws).send(json.dumps({"s": 2, "sn": self._sn}))  # type: ignore[attr-defined]
            try:
                await asyncio.wait_for(self._pong.wait(), timeout=gap)
            except TimeoutError:
                continue
            return True
        return False

    async def _heartbeat(self) -> None:
        """定期发 signal 2（ping，带最近 sn）并等 pong。

        官方口径：发出 ping 后**6 秒内没收到 pong 就是超时**；超时先按官方补两次探活 ping
        （间隔 2、4），探活也不回才主动断开、交给 :meth:`_run_loop` 重连 —— 连接假死
        （对端不发也不关）只能靠这一条发现。
        """
        while True:
            await asyncio.sleep(self._heartbeat_interval())
            ws = self._ws
            if ws is None:
                continue
            self._pong.clear()
            try:
                await cast(object, ws).send(json.dumps({"s": 2, "sn": self._sn}))  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 — 发不出去由下面的超时 / 收报文循环兜底
                self._log.debug("kook 心跳发送失败", error=str(exc))
                continue
            try:
                await asyncio.wait_for(self._pong.wait(), timeout=_PONG_TIMEOUT)
            except TimeoutError:
                self._log.warning("kook 心跳超时，先探活再决定重连", timeout=_PONG_TIMEOUT)
            else:
                continue
            if not await self._probe_connection():
                await self._close_current()

    async def _handle_raw(self, raw: str | bytes) -> None:
        """一条原始报文：解 JSON -> hello 记 session_id、事件按顶层 sn 推进并去重。"""
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        try:
            loaded = cast(object, json.loads(text))
        except ValueError:
            self._log.warning("kook 收到非 JSON 文本，已忽略")
            return
        if not isinstance(loaded, dict):
            self._log.warning("kook 收到非对象 JSON，已忽略")
            return
        signal = loaded.get("s")
        data = loaded.get("d")
        if signal == 1:  # hello：记下 session_id（断线重连 resume 要用），立刻 ping 一次
            code = data.get("code", 0) if isinstance(data, dict) else 0
            if code != 0:
                # 握手失败（token 无效 / 过期）/ 续传被拒：清掉续传状态并**主动断开**，
                # 下轮重连走全新连接；干等网关掐线的话，这条死连接会一直挂着
                self._log.warning(
                    "kook 握手失败（hello code=%s），主动断开后全新连接", code=code
                )
                self._session_id = ""
                self._sn = 0
                self._gateway_url = ""
                await self._close_current()
                return
            if isinstance(data, dict) and isinstance(data.get("session_id"), str):
                self._session_id = data["session_id"]
            self._greeted = True
            self._ping_now()
            return
        if signal == 5:  # reconnect：服务端宣布这条连接已失效，客户端应主动断开重连
            code = data.get("code") if isinstance(data, dict) else None
            self._log.warning("kook 网关要求重连（reconnect），主动断开", code=code)
            # 官方口径：重新获取 gateway + 清空 sn + 清空消息队列，否则会消息错乱
            self._session_id = ""
            self._sn = 0
            self._gateway_url = ""
            self._drain_inbox()
            await self._close_current()
            return
        if signal == 3:  # pong：心跳回应，不推进 sn（但要把心跳那条腿放行）
            self._pong.set()
            return
        if signal == 6:  # resume ack：续传成功，服务端可能下发新的 session_id
            if isinstance(data, dict) and isinstance(data.get("session_id"), str):
                self._session_id = data["session_id"]
            self._greeted = True  # 能续传说明这条连接是好的
            return
        if signal != 0 or not isinstance(data, dict):
            return
        # 事件报文：sn 在顶层（官方协议 {"s":0,"d":{...},"sn":N}），是「已收到」的推进点。
        # 心跳 ping 带上最新 sn，网关才确认事件送达；不推进的话网关会按旧 sn 反复重传。
        event_sn = loaded.get("sn")
        if not isinstance(event_sn, int):
            event_sn = None
        if event_sn is not None and event_sn <= self._sn:
            # 已处理过的 sn（重连重放 / 重传）：直接丢弃，别重复投递
            self._log.debug("kook 事件 sn 已处理过，丢弃", sn=event_sn)
            return
        if event_sn is not None:
            self._sn = event_sn
        try:
            event = parse_event(cast("Mapping[str, object]", data))
        except ValidationError as exc:
            self._log.warning("kook 事件解析失败，已忽略", error=str(exc))
            return
        if event.self_id and not self.self_id:
            self.self_id = event.self_id
        # 入队（有界，满了会阻塞收报文这条腿 = 背压）；worker 按序取出来交给 handler
        await self._inbox.put(event)

    async def _consume(self) -> None:
        """worker：按序把事件交给 handler（一条处理完再下一条，业务不用自己排队）。"""
        while True:
            event = await self._inbox.get()
            if self._handler is None:
                self._log.debug("kook 未注册事件处理器，事件已丢弃", type=event.type)
                continue
            try:
                await self._handler(event)
            except Exception:
                self._log.exception("kook 事件处理器抛出异常", type=event.type)

    def _ping_now(self) -> None:
        """立即发一次 ping（hello 之后）。"""
        ws = self._ws
        if ws is None:
            return

        async def _send() -> None:
            try:
                await cast(object, ws).send(json.dumps({"s": 2, "sn": self._sn}))  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                self._log.debug("kook 心跳发送失败", error=str(exc))

        asyncio.get_running_loop().create_task(_send())

    # ------------------------------------------------------------------ 动作（REST）
    async def call(self, action: str, /, **params: object) -> KookActionResponse:
        """发一个动作（走 REST API，带 Bot Token），返回规范化回应。

        ``action`` 是 Kook 的 REST 动作名（如 ``send_channel_msg``），由适配器映射到具体端点；
        本层只负责「带凭证 POST 到 REST、把回应解析成 :class:`KookActionResponse`」。

        :raises ValueError: 动作不在注册表 / 缺必填参数 / 有不认识的参数（能力表拦下）；
        :raises ConnectionError: 没配 Token / 网络失败等环境问题。
        """
        token = self._options.token
        if not token:
            raise ConnectionError("[kook] 未配置 Bot Token，无法发动作")
        # 动作级校验：先查能力表（动作名拼错当场炸），再验参数（必填缺失 / 未知参数都拦下）
        spec = _action_spec(action)
        missing = [name for name in spec.required if name not in params]
        if missing:
            raise ValueError(f"[kook] 动作 {action!r} 缺必填参数：{'、'.join(missing)}")
        unknown = [name for name in params if name not in spec.required and name not in spec.optional]
        if unknown:
            raise ValueError(f"[kook] 动作 {action!r} 有不认识的参数：{'、'.join(unknown)}")
        path = spec.endpoint
        url = _api_url(self._options, path)
        body = json.dumps(params, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
        }
        try:
            payload = await self._rest("POST", url, headers=headers, body=body)
        except ConnectionError as exc:
            raise ConnectionError(f"[kook] 动作 {action!r} 发送失败：{exc}") from exc
        return parse_action_response(payload)


class KookAction:
    """一个 Kook REST 动作的「能力表面」：端点 + 参数契约。

    端点（``endpoint``）是发到 Kook REST 的路径；参数分**必填**（``required``，缺了
    当场抛）与**可选**（``optional``，预留：Kook 的 create 端点还有 target_type / type
    一类可选字段，将来要用就在这里声明）。加动作 = 在这里加一行，``call`` / ``_action_path``
    都不用改。
    """

    __slots__ = ("name", "endpoint", "required", "optional")

    def __init__(
        self,
        name: str,
        endpoint: str,
        required: tuple[str, ...],
        optional: tuple[str, ...] = (),
    ) -> None:
        self.name: str = name
        self.endpoint: str = endpoint
        self.required: tuple[str, ...] = required
        self.optional: tuple[str, ...] = optional


#: 动作注册表（能力表面）：动作名 -> 端点 + 参数契约
KOOK_ACTIONS: dict[str, KookAction] = {
    "send_channel_msg": KookAction("send_channel_msg", "/message/create", ("target_id", "content")),
    # 私聊是另一套端点（官方 /api/v3/direct-message/create）：/message/create 只认频道号
    "send_dm_msg": KookAction("send_dm_msg", "/direct-message/create", ("target_id", "content")),
    "delete_msg": KookAction("delete_msg", "/message/delete", ("msg_id",)),
}


def _action_spec(action: str) -> KookAction:
    """按动作名取能力表；不认识的动作**当场抛**（拼错动作名调用时就炸，不兜底到 create）。"""
    spec = KOOK_ACTIONS.get(action)
    if spec is None:
        raise ValueError(
            f"[kook] 动作 {action!r} 不在注册表里（可选：{'、'.join(KOOK_ACTIONS)}）"
        )
    return spec


def _action_path(action: str) -> str:
    """动作名 -> Kook REST 端点路径（查能力表；不认识的动作当场抛）。"""
    return _action_spec(action).endpoint
