"""Kook 正向 WebSocket 客户端：连 Kook 网关、收事件、发动作。

与 OneBot 相反的方向：OneBot 是**反向 WS**（框架当服务端），这里是**正向 WS**（框架当
客户端，主动连 Kook 网关，用 Bot Token 鉴权）。所以本模块的核心是「建连 + 心跳保活 +
收事件分发 + 断线重连」，而不是「监听端口等实现连进来」。

收发规则（Kook 网关）::

    框架 -> 网关：signal 2（ping，带最近 sn）；或 REST API（发消息等动作）
    网关 -> 框架：signal 1（hello）/ signal 0（事件）/ signal 3（pong）

Kook 的正向 WS **只推事件，不能发消息**；发消息走 **REST API**（HTTP POST，带 Bot Token）。
所以 :meth:`KookClient.call` 走 HTTP，而不是 WS —— 这是与 OneBot 的另一个关键差异。

本模块只管「连接 + 协议」这一层：建连鉴权、心跳保活、事件分发（``handler`` 钩子）、
动作发送、断线重连；**业务不在这里**。事件进 ``handler``：``async def on_event(event) -> None``。

用法::

    from nacho.kook import KookOptions, KookClient

    async def on_event(event):
        print(event.type, event.target_id, event.content)

    client = KookClient(KookOptions(token="xxx"), handler=on_event)
    await client.start()
    await client.serve_forever()
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from typing import TypeAlias, cast
from urllib import request as urlreq

from pydantic import ValidationError
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from nacho.core.logger import BaseLogger

from .logging import KOOK_LOGGER_NAME, kook_logger
from .models import KookActionResponse, KookEvent, parse_action_response, parse_event
from .options import KookOptions

#: 事件处理器：收到一条事件时的回调。抛出的异常只会被记下来，不影响后续事件。
EventHandler: TypeAlias = Callable[[KookEvent], Awaitable[None]]

#: REST API 基地址（发消息等动作走这里，不是 WS）
_DEFAULT_API: str = "https://www.kookapp.cn/api/v3"


def _api_url(options: KookOptions, path: str) -> str:
    """拼 REST 端点的完整地址。"""
    return f"{_DEFAULT_API}{path}"


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
        self._reconnect_task: asyncio.Task[None] | None = None
        self._stopping: bool = False
        #: 最近一次 hello / pong 里的 sn（心跳 ping 要带上）
        self._sn: int = 0
        #: 连上后从事件里学到的机器人自身 id；没学到是空串
        self.self_id: str = ""

    @property
    def options(self) -> KookOptions:
        """当前生效的选项。"""
        return self._options

    # ------------------------------------------------------------------ 连接
    async def start(self) -> None:
        """开始连接（幂等：已经连着就什么都不做）。"""
        if self._ws is not None:
            return
        self._stopping = False
        self._reconnect_task = asyncio.create_task(self._run_loop(), name="kook-connect")

    async def serve_forever(self) -> None:
        """起连接并一直等到被停（``stop`` 或进程被中断）。"""
        await self.start()
        await self._wait_until_stopped()

    async def _wait_until_stopped(self) -> None:
        """挂起直到 ``stop()`` 被调用；用于 serve_forever 的退出条件。"""
        while not self._stopping:
            await asyncio.sleep(0.1)

    async def stop(self) -> None:
        """停客户端：关连接、停心跳、停重连（幂等）。"""
        self._stopping = True
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

    # ------------------------------------------------------------------ 收发循环
    async def _run_loop(self) -> None:
        """连接 -> 心跳 -> 收报文；断了按间隔重连，直到 ``stop``。"""
        while not self._stopping:
            try:
                await self._connect_once()
            except Exception as exc:  # noqa: BLE001 — 连不上就退出来等下轮重连
                self._log.warning("kook 连接失败，稍后重连", error=str(exc))
            if self._stopping:
                return
            await asyncio.sleep(self._options.reconnect_interval)

    async def _connect_once(self) -> None:
        """建一条连接并跑它的收报文循环；连接断开 / 出错时返回（由 _run_loop 决定重连）。"""
        gateway = self._options.gateway
        token = self._options.token
        self._log.info("kook 正在连接网关", gateway=gateway, token_set=bool(token))
        async with ws_connect(
            gateway,
            additional_headers={"Authorization": f"Bot {token}"} if token else None,
            logger=None,
        ) as ws:
            self._ws = ws
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat(), name="kook-heartbeat"
            )
            self._log.info("kook 已连接网关", gateway=gateway)
            try:
                async for raw in ws:
                    self._handle_raw(cast("str | bytes", raw))
            finally:
                if self._heartbeat_task is not None:
                    self._heartbeat_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await self._heartbeat_task
                    self._heartbeat_task = None
                self._ws = None

    async def _heartbeat(self) -> None:
        """定期发 signal 2（ping，带最近 sn），保活连接。"""
        interval = self._options.heartbeat_interval
        while True:
            await asyncio.sleep(interval)
            ws = self._ws
            if ws is None:
                continue
            try:
                await cast(object, ws).send(json.dumps({"s": 2, "sn": self._sn}))  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 — 心跳失败由收报文循环兜底
                self._log.debug("kook 心跳发送失败", error=str(exc))

    def _handle_raw(self, raw: str | bytes) -> None:
        """一条原始报文：解 JSON -> signal 1/3 更新 sn、signal 0 交给 handler。"""
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
        if signal in (1, 3):  # hello / pong：更新 sn，交给心跳任务带上下一次 ping
            if isinstance(data, dict) and isinstance(data.get("sn"), int):
                self._sn = int(data["sn"])
            # hello 后立刻 ping 一次，别等心跳周期
            if signal == 1:
                self._ping_now()
            return
        if signal != 0 or not isinstance(data, dict):
            return
        try:
            event = parse_event(cast("Mapping[str, object]", data))
        except ValidationError as exc:
            self._log.warning("kook 事件解析失败，已忽略", error=str(exc))
            return
        if event.self_id and not self.self_id:
            self.self_id = event.self_id
        if self._handler is None:
            self._log.debug("kook 未注册事件处理器，事件已丢弃", type=event.type)
            return
        try:
            # handler 是 async；_handle_raw 是同步的，得安排进事件循环
            asyncio.get_running_loop().create_task(self._emit(event))
        except Exception:  # noqa: BLE001
            self._log.exception("kook 事件处理器抛出异常", type=event.type)

    async def _emit(self, event: KookEvent) -> None:
        """把事件交给业务钩子；handler 抛异常只记日志。"""
        if self._handler is None:
            return
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

        :raises ConnectionError: 没配 Token / 网络失败等环境问题。
        """
        token = self._options.token
        if not token:
            raise ConnectionError("[kook] 未配置 Bot Token，无法发动作")
        path = _action_path(action)
        url = _api_url(self._options, path)
        body = json.dumps(params, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
        }

        def _post() -> Mapping[str, object]:
            req = urlreq.Request(url, data=body, headers=headers, method="POST")
            with urlreq.urlopen(req, timeout=self._options.action_timeout) as resp:
                return cast("Mapping[str, object]", json.loads(resp.read().decode("utf-8")))

        try:
            payload = await asyncio.to_thread(_post)
        except Exception as exc:  # noqa: BLE001 — 网络 / HTTP 错误统一按环境问题抛
            raise ConnectionError(f"[kook] 动作 {action!r} 发送失败：{exc}") from exc
        return parse_action_response(payload)


def _action_path(action: str) -> str:
    """动作名 -> Kook REST 端点路径（发消息类都走 message/create，删除走 message/delete）。"""
    return {
        "send_channel_msg": "/message/create",
        "send_dm_msg": "/message/create",
        "delete_msg": "/message/delete",
    }.get(action, "/message/create")
