"""OneBot 适配器的测试：事件翻译、能力转述、兼容面透传（跑真 WS，不 mock）。

套路与 ``test_onebot.py`` 同源：127.0.0.1 空闲端口 + ``websockets`` 客户端；令牌注册表
不配（匿名模式）——翻译与路由不依赖归属语义，留一条空归属的连接就够。需要
``websockets``（``pip install "tickneko[onebot]"``），没装就整文件跳过。
"""
from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import cast

import pytest

pytest.importorskip("websockets", reason="OneBot 接入层要装 websockets：pip install \"tickneko[onebot]\"")

from websockets.asyncio.client import connect

from tickneko.platforms.bridge import Gateway
from tickneko.platforms.bridge.models import PlatformEvent
from tickneko.platforms.bridge.onebot import OneBotAdapter, OneBotTarget
from tickneko.platforms.onebot import OneBotOptions

#: 一条私聊消息事件（同 test_onebot.py 的形状）
PRIVATE_MESSAGE: dict[str, object] = {
    "post_type": "message",
    "message_type": "private",
    "time": 1_700_000_000,
    "self_id": 10001,
    "user_id": 20002,
    "raw_message": "你好",
    "message": "你好",
    "sender": {"nickname": "对方"},
}

#: 一条心跳（不该出现在总线上）
HEARTBEAT: dict[str, object] = {
    "post_type": "meta_event",
    "meta_event_type": "heartbeat",
    "time": 1_700_000_001,
    "self_id": 10001,
    "interval": 5,
}


def free_port() -> int:
    """挑一个当前空闲的端口（让内核分配，测完即释放）。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return cast("tuple[str, int]", sock.getsockname())[1]


def ws_url(port: int) -> str:
    """反向 WS 地址（匿名模式：不带令牌）。"""
    return f"ws://127.0.0.1:{port}/"


async def wait_until(predicate, timeout: float = 3.0) -> bool:
    """轮询等一个条件成立（服务端那条腿是异步跑的，得给它一点时间）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


@asynccontextmanager
async def served_adapter() -> AsyncGenerator[tuple[Gateway, OneBotAdapter, list[PlatformEvent]], None]:
    """起一套「Gateway + OneBot 适配器 + 收件订阅」，收件箱供断言；退出时停干净。"""
    gateway = Gateway()
    inbox: list[PlatformEvent] = []

    async def collect(event: PlatformEvent) -> None:
        inbox.append(event)

    gateway.subscribe(collect)
    adapter = OneBotAdapter(
        OneBotOptions(host="127.0.0.1", port=free_port()), publish=gateway.publish
    )
    gateway.register(adapter)
    await gateway.start()
    try:
        yield gateway, adapter, inbox
    finally:
        await gateway.stop()


# ---------------------------------------------------------------------- 事件翻译
async def test_message_event_translated() -> None:
    """私聊消息 -> 规范化事件：身份转字符串、会话指向归一、raw 兜底整条原始事件。"""
    async with served_adapter() as (_gateway, adapter, inbox):
        port = adapter.server.options.port
        async with connect(ws_url(port)) as ws:
            await ws.send(json.dumps(PRIVATE_MESSAGE))
            assert await wait_until(lambda: len(inbox) == 1)

        event = inbox[0]
        assert event.platform == "onebot"
        assert event.owner_id == ""  # 匿名模式：归属空串
        assert event.self_id == "10001"  # 数字转字符串
        assert event.kind == "message"
        assert event.chat == "private"
        assert event.chat_id == "20002"  # 私聊的会话指向就是对方
        assert event.user_id == "20002"
        assert event.text == "你好"
        assert event.time == 1_700_000_000.0
        # raw 兜底：整条原始事件（消息段数组 / sender 这些翻译不了的字段从这里拿）
        assert event.raw is not None
        assert event.raw.sender == {"nickname": "对方"}  # type: ignore[attr-defined]
        # 会话定位是 OneBot 自己的 target：号保留整数（免字符串往返）
        assert isinstance(event.target, OneBotTarget)
        assert event.target.chat == "private"
        assert event.target.user_id == 20002


async def test_heartbeat_not_published() -> None:
    """心跳到不了总线（服务端 _emit 已滤；适配器不重复滤也不放进来）。"""
    async with served_adapter() as (_gateway, adapter, inbox):
        port = adapter.server.options.port
        async with connect(ws_url(port)) as ws:
            await ws.send(json.dumps(HEARTBEAT))
            await asyncio.sleep(0.2)  # 给它足够的时间「不发」
        assert inbox == []


async def test_group_notice_translated() -> None:
    """群通知：带 group_id 算群事件，chat_id 取群号。"""
    async with served_adapter() as (_gateway, adapter, inbox):
        port = adapter.server.options.port
        async with connect(ws_url(port)) as ws:
            await ws.send(
                json.dumps(
                    {
                        "post_type": "notice",
                        "notice_type": "group_increase",
                        "time": 1_700_000_002,
                        "self_id": 10001,
                        "user_id": 20002,
                        "group_id": 70001,
                    }
                )
            )
            assert await wait_until(lambda: len(inbox) == 1)
        event = inbox[0]
        assert event.kind == "notice"
        assert event.chat == "group"
        assert event.chat_id == "70001"
        assert event.user_id == "20002"
        assert event.text == ""


async def test_recall_notice_carries_message_id() -> None:
    """撤回通知：message_id 是额外字段，规范化事件也要带上（下游不用下探 raw）。"""
    async with served_adapter() as (_gateway, adapter, inbox):
        port = adapter.server.options.port
        async with connect(ws_url(port)) as ws:
            await ws.send(
                json.dumps(
                    {
                        "post_type": "notice",
                        "notice_type": "friend_recall",
                        "time": 1_700_000_003,
                        "self_id": 10001,
                        "user_id": 20002,
                        "message_id": 123456,
                    }
                )
            )
            assert await wait_until(lambda: len(inbox) == 1)
        event = inbox[0]
        assert event.kind == "notice"
        assert event.chat == "other"  # 私聊撤回没有 group_id
        assert event.message_id == "123456"  # 额外字段也翻上来了
        assert event.user_id == "20002"


# ---------------------------------------------------------------------- 能力转述
async def test_send_via_gateway_roundtrip() -> None:
    """Gateway 路由发送：适配器挑连接、客户端收到动作、回执翻成 ActionResult。"""
    async with served_adapter() as (gateway, adapter, _inbox):
        port = adapter.server.options.port
        async with connect(ws_url(port)) as ws:
            await wait_until(lambda: len(adapter.connections) == 1)
            # 客户端那头：收到动作就按 echo 答一条成功回执
            answered = asyncio.get_running_loop().create_future()

            async def answer() -> None:
                raw = await ws.recv()
                payload = json.loads(raw)
                await ws.send(
                    json.dumps(
                        {
                            "status": "ok",
                            "retcode": 0,
                            "data": {"message_id": 7},
                            "echo": payload["echo"],
                        }
                    )
                )
                answered.set_result(None)

            worker = asyncio.create_task(answer())
            result = await gateway.send(
                "onebot", "", "send_private_msg", user_id=20002, message="hi"
            )
            await asyncio.wait_for(answered, 3.0)
            await asyncio.wait_for(worker, 3.0)

        assert result.ok is True
        assert result.data == {"message_id": 7}


async def test_reply_roundtrip_private() -> None:
    """回复到私聊会话：按 target 原样回私聊（send_private_msg，号已是整数）。"""
    async with served_adapter() as (gateway, adapter, _inbox):
        port = adapter.server.options.port
        async with connect(ws_url(port)) as ws:
            await wait_until(lambda: len(adapter.connections) == 1)
            answered = asyncio.get_running_loop().create_future()

            async def answer() -> None:
                raw = await ws.recv()
                payload = json.loads(raw)
                await ws.send(
                    json.dumps(
                        {
                            "status": "ok",
                            "retcode": 0,
                            "data": {"message_id": 8},
                            "echo": payload["echo"],
                        }
                    )
                )
                answered.set_result(None)

            worker = asyncio.create_task(answer())
            target = OneBotTarget(owner_id="", chat="private", user_id=20002)
            result = await gateway.reply(target, "收到，马上办")
            await asyncio.wait_for(answered, 3.0)
            await asyncio.wait_for(worker, 3.0)

        assert result.ok is True
        assert result.data == {"message_id": 8}


async def test_reply_group_uses_group_id() -> None:
    """回复到群聊会话：send_group_msg，会话定位里的 group_id 即群号（整数，不用转）。"""
    async with served_adapter() as (gateway, adapter, _inbox):
        port = adapter.server.options.port
        async with connect(ws_url(port)) as ws:
            await wait_until(lambda: len(adapter.connections) == 1)
            got_action: dict[str, object] = {}

            async def answer() -> None:
                raw = await ws.recv()
                payload = json.loads(raw)
                got_action.update(
                    {
                        "action": payload.get("action", ""),
                        "params": payload.get("params", {}),
                    }
                )
                await ws.send(
                    json.dumps(
                        {
                            "status": "ok",
                            "retcode": 0,
                            "data": {},
                            "echo": payload["echo"],
                        }
                    )
                )

            worker = asyncio.create_task(answer())
            target = OneBotTarget(owner_id="", chat="group", group_id=70001, user_id=20002)
            await gateway.reply(target, "群里的回复")
            await asyncio.wait_for(worker, 3.0)

        assert got_action["action"] == "send_group_msg"
        assert got_action["params"] == {"group_id": 70001, "message": "群里的回复"}


def test_make_target_builds_platform_target() -> None:
    """手动构造回程地址：通用会话字段 -> OneBot 的号（转整数），协议口径一致。"""
    adapter = OneBotAdapter(OneBotOptions(port=free_port()))
    group = adapter.make_target(owner_id="", chat="group", chat_id="70001")
    assert isinstance(group, OneBotTarget)
    assert group.chat == "group" and group.group_id == 70001
    assert group.message_id is None

    private = adapter.make_target(owner_id="", chat="private", user_id="20002", message_id="9")
    assert private.chat == "private" and private.user_id == 20002
    assert private.message_id == 9

    fallback = adapter.make_target(owner_id="", chat="private", chat_id="20003")
    assert fallback.user_id == 20003  # 没给 user_id 时回退 chat_id


async def test_send_without_online_connection_raises() -> None:
    """环境问题当场抛：归属下没有在线连接（口径同 onebot 节点）。"""
    async with served_adapter() as (gateway, _adapter, _inbox):
        with pytest.raises(ConnectionError, match="没有归属"):
            await gateway.send("onebot", "u-nobody", "send_private_msg", user_id=1, message="hi")


async def test_clients_snapshot() -> None:
    """在线列表转述：roster 的 int 口径翻成 BotClient 的字符串口径。"""
    async with served_adapter() as (_gateway, adapter, _inbox):
        port = adapter.server.options.port
        async with connect(ws_url(port)) as ws:
            await wait_until(lambda: len(adapter.connections) == 1)
            # 还没发事件：self_id 没学到，是空串
            assert adapter.clients()[0].self_id == ""
            await ws.send(json.dumps(PRIVATE_MESSAGE))
            assert await wait_until(lambda: adapter.clients()[0].self_id == "10001")

            client = adapter.clients()[0]
            assert client.owner_id == ""  # 匿名
            assert client.remote.startswith("127.0.0.1:")
            assert client.connected_at > 0


# ---------------------------------------------------------------------- 兼容面
async def test_compat_surface_passthrough() -> None:
    """兼容面：tokens / connections / roster 透传给被包的服务端（下游零改动的验收线）。"""
    async with served_adapter() as (_gateway, adapter, _inbox):
        assert adapter.tokens is None  # 匿名模式：没配注册表
        port = adapter.server.options.port
        async with connect(ws_url(port)):
            await wait_until(lambda: len(adapter.connections) == 1)
            # roster 是**平台口径**（self_id 仍 int | None），与 BotClient 的字符串口径分开
            assert len(adapter.roster()) == 1
            assert adapter.roster()[0].self_id is None
            assert adapter.roster()[0] == adapter.server.roster()[0]  # 透传：同一份快照语义
            assert await adapter.kick("不存在的连接") is False  # 透传：没这条连接
        assert await wait_until(lambda: adapter.connections == ())


async def test_gateway_lifecycle_controls_server() -> None:
    """生命周期经总线：gateway.stop() 后端口关掉、连接清空。"""
    async with served_adapter() as (_gateway, adapter, _inbox):
        port = adapter.server.options.port
        async with connect(ws_url(port)):
            await wait_until(lambda: len(adapter.connections) == 1)
        # served_adapter 退出时会 gateway.stop()；这里复起一次验证 start/stop 幂等循环
        await adapter.start()
        async with connect(ws_url(port)):
            assert await wait_until(lambda: len(adapter.connections) == 1)
        await adapter.stop()
        assert adapter.connections == ()


# ---------------------------------------------------------------------- 翻译兜底
class _UnknownEvent:
    """不属于四类已知事件的形状：模拟 OneBotEvent 联合将来扩出的新类别。"""

    self_id = 10001
    time = 0


class _StubConn:
    """_translate 只读 conn.id，够用即可。"""

    id = "u-admin"


def test_translate_unknown_event_type_raises() -> None:
    """翻译穷举不了的事件类型：当场 TypeError，绝不静默归成 meta（-O 下 assert 会被剥）。"""
    from tickneko.platforms.bridge.onebot import _translate

    with pytest.raises(TypeError, match="未认识"):
        _translate(_StubConn(), _UnknownEvent())  # type: ignore[arg-type]
