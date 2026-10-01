"""桥接层测试：规范化模型（P2-1）、协议（P2-2）、Gateway（P2-3）、OneBot 适配器（P2-4）。

这一份先覆盖**模型口径**——身份字段统一字符串、`raw` 兜底、冻结不可改；后续小节
随实现逐段长出来。
"""
from __future__ import annotations

import dataclasses

import pytest

from nacho.platforms.bridge import Gateway
from nacho.platforms.bridge.models import ActionResult, BotClient, ChatTarget, PlatformEvent
from nacho.platforms.bridge.protocols import BotAdapter


# --------------------------------------------------------------------- 模型口径
def test_platform_event_defaults() -> None:
    """最小事件（只给身份）：其余字段都有确定的空口径，不留 None 悬空。"""
    event = PlatformEvent(platform="onebot", owner_id="u-admin")
    assert event.self_id == ""
    assert event.kind == "meta"
    assert event.chat == "other"
    assert event.chat_id == ""
    assert event.user_id == ""
    assert event.text == ""
    assert event.message_id == ""
    assert event.time == 0.0
    assert event.raw is None


def test_platform_event_message_shape() -> None:
    """消息事件：私聊指向的会话就是对方账号；身份数字转字符串。"""
    raw = {"post_type": "message", "user_id": 10086}
    event = PlatformEvent(
        platform="onebot",
        owner_id="u-admin",
        self_id="10001",
        kind="message",
        chat="private",
        chat_id="10086",
        user_id="10086",
        text="你好",
        message_id="7",
        time=1_728_000_000.0,
        raw=raw,
    )
    # 数字平台转字符串后，下游不用再 isinstance 分岔
    assert event.self_id == "10001" and event.user_id == "10086"
    assert event.chat == "private" and event.chat_id == "10086"
    assert event.text == "你好"
    # raw 是整条原始引用：翻译不了的字段从这里兜
    assert event.raw is raw


def test_platform_event_frozen() -> None:
    """事件是已发生的事：改写要当场被拦。"""
    event = PlatformEvent(platform="onebot", owner_id="u-admin")
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.kind = "message"  # type: ignore[misc]


@dataclasses.dataclass(frozen=True)
class _ChatTarget:
    """测试用最小 target：按「结构化满足 ChatTarget 协议」长（platform 是协议承诺）。

    平台特有的定位字段（chat / chat_id / user_id / message_id）由**各适配器自己定义**
    （如 OneBot 的 ``OneBotTarget``、Kook 的 ``KookTarget``）；这一份只是协议测试用的
    最小实现，验证「只认路由键、形状自便」的兼容面。
    """

    platform: str
    owner_id: str
    chat: str = "other"
    chat_id: str = ""
    user_id: str = ""
    message_id: str = ""


def test_platform_event_target_carries_session_location() -> None:
    """事件自带会话定位（target）：回复时原样传回就能回同一会话。

    target 是**适配器翻译时塞进来的平台特有对象**（不再从规范化字段派生）——这里用
    测试的 ``_ChatTarget`` 模拟「产 target 的适配器」，断言它原样挂在事件上。
    """
    target = _ChatTarget(platform="onebot", owner_id="u-admin", chat="group", chat_id="123456")
    event = PlatformEvent(
        platform="onebot",
        owner_id="u-admin",
        kind="message",
        chat="group",
        chat_id="123456",
        user_id="10086",
        message_id="7",
        target=target,
    )
    assert event.target is target  # 原样回传，字段形状由产它的适配器定


def test_chat_target_protocol_minimal_contract() -> None:
    """ChatTarget 协议只承诺路由键：带 platform 就被认成协议，其余字段自便。"""
    assert isinstance(_ChatTarget(platform="kook", owner_id="u-admin"), ChatTarget)
    assert not isinstance(object(), ChatTarget)


def test_bot_client_minimal() -> None:
    """在线列表一行：最小只需两个身份字段，其余空口径。"""
    client = BotClient(client_id="c1", owner_id="u-admin")
    assert client.account == ""
    assert client.self_id == ""
    assert client.remote == ""
    assert client.connected_at == 0.0


def test_action_result_ok_and_failure() -> None:
    """回执：成功只看 ok；失败带说明，细节下探 raw。"""
    ok = ActionResult(ok=True, data={"message_id": 7})
    assert ok.ok is True and ok.message == "" and ok.data == {"message_id": 7}
    failed = ActionResult(ok=False, message="retcode=1200", raw={"retcode": 1200})
    assert failed.ok is False and failed.message == "retcode=1200"
    assert failed.raw == {"retcode": 1200}


# ---------------------------------------------------------------------- 协议形状
class _DuckAdapter:
    """鸭子适配器：不继承任何东西，按 BotAdapter 的形状长。"""

    platform = "duck"

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def clients(self, *, owner_id: str | None = None) -> tuple[BotClient, ...]:
        return ()

    async def send(self, owner_id: str, action: str, /, **params: object) -> ActionResult:
        raise ConnectionError("没有连接")

    async def reply(self, target: ChatTarget, content: str) -> ActionResult:
        raise ConnectionError("没有连接")


def test_duck_adapter_satisfies_protocol() -> None:
    """结构化满足：没继承协议、按形状长就能被认成 BotAdapter。"""
    adapter = _DuckAdapter()
    assert isinstance(adapter, BotAdapter)


def test_plain_object_not_an_adapter() -> None:
    """反面：缺方法的对象不是适配器（runtime_checkable 的意义）。"""
    assert not isinstance(object(), BotAdapter)


# ------------------------------------------------------------------------ 总线
class _FakeAdapter:
    """记账适配器：记下生命周期与发送请求，供断言。"""

    def __init__(self, platform: str, *, online: tuple[BotClient, ...] = ()) -> None:
        self.platform = platform
        self._online = online
        self.calls: list[tuple[str, str, dict[str, object]]] = []  # (owner, action, params)
        self.replies: list[tuple[ChatTarget, str]] = []  # (target, content)
        self.started = 0
        self.stopped = 0

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1

    def clients(self, *, owner_id: str | None = None) -> tuple[BotClient, ...]:
        if owner_id is None:
            return self._online
        return tuple(c for c in self._online if c.owner_id == owner_id)

    async def send(self, owner_id: str, action: str, /, **params: object) -> ActionResult:
        self.calls.append((owner_id, action, params))
        return ActionResult(ok=True, data={"echo_of": action})

    async def reply(self, target: ChatTarget, content: str) -> ActionResult:
        self.replies.append((target, content))
        return ActionResult(ok=True, data={"reply_to": target.chat_id})


async def test_gateway_publish_reaches_subscribers_in_order() -> None:
    """事件从 publish 口进来，订阅者按订阅顺序依次收到同一条。"""
    gateway = Gateway()
    got_first: list[PlatformEvent] = []
    got_second: list[PlatformEvent] = []

    async def first(event: PlatformEvent) -> None:
        got_first.append(event)

    async def second(event: PlatformEvent) -> None:
        got_second.append(event)

    gateway.subscribe(first)
    gateway.subscribe(second)

    event = PlatformEvent(platform="onebot", owner_id="u-admin", kind="message", text="hi")
    await gateway.publish(event)

    assert got_first == [event] and got_second == [event]


async def test_gateway_subscriber_exception_does_not_sink_the_bus() -> None:
    """异常口径：一个订阅者抛异常，后面的照常收到，publish 也不向上抛。"""
    gateway = Gateway()
    got: list[PlatformEvent] = []

    async def bad(_event: PlatformEvent) -> None:
        raise RuntimeError("业务炸了")

    async def good(event: PlatformEvent) -> None:
        got.append(event)

    gateway.subscribe(bad)
    gateway.subscribe(good)
    await gateway.publish(PlatformEvent(platform="onebot", owner_id="u-admin"))
    assert got  # 第二个订阅者不受牵连


async def test_gateway_register_rejects_duplicate_platform() -> None:
    """平台标识是路由键：重复注册当场 ValueError。"""
    gateway = Gateway()
    gateway.register(_FakeAdapter("onebot"))
    with pytest.raises(ValueError, match="onebot"):
        gateway.register(_FakeAdapter("onebot"))
    assert gateway.platforms == ("onebot",)


async def test_gateway_send_routes_by_platform() -> None:
    """发送路由：按平台找到适配器，参数原样转述，回执透传。"""
    gateway = Gateway()
    onebot = _FakeAdapter("onebot")
    kook = _FakeAdapter("kook")
    gateway.register(onebot)
    gateway.register(kook)

    result = await gateway.send("kook", "u-admin", "send_msg", message="hi", user_id="42")

    assert result.ok is True and result.data == {"echo_of": "send_msg"}
    assert kook.calls == [("u-admin", "send_msg", {"message": "hi", "user_id": "42"})]
    assert onebot.calls == []  # 路由只去 kook


async def test_gateway_send_unknown_platform_raises() -> None:
    """环境问题当场抛：没注册的平台 ConnectionError，消息里带已注册列表。"""
    gateway = Gateway()
    gateway.register(_FakeAdapter("onebot"))
    with pytest.raises(ConnectionError, match="onebot"):
        await gateway.send("kook", "u-admin", "send_msg")


async def test_gateway_reply_routes_by_target_platform() -> None:
    """回复路由：按 target.platform 回到原适配器，内容原样转述。"""
    gateway = Gateway()
    onebot = _FakeAdapter("onebot")
    kook = _FakeAdapter("kook")
    gateway.register(onebot)
    gateway.register(kook)

    target = _ChatTarget(
        platform="kook", owner_id="u-admin", chat="group", chat_id="ch-1", user_id="42"
    )
    result = await gateway.reply(target, "收到，马上办")

    assert result.ok is True and result.data == {"reply_to": "ch-1"}
    assert kook.replies == [(target, "收到，马上办")]
    assert onebot.replies == []  # 路由只去 kook


async def test_gateway_reply_unknown_platform_raises() -> None:
    """回复到没注册的平台：ConnectionError，消息里带已注册列表。"""
    gateway = Gateway()
    gateway.register(_FakeAdapter("onebot"))
    with pytest.raises(ConnectionError, match="kook"):
        await gateway.reply(
            _ChatTarget(platform="kook", owner_id="u-admin", chat="group", chat_id="ch-1"),
            "hi",
        )


async def test_gateway_lifecycle_order() -> None:
    """生命周期：start 按注册顺序、stop 按逆序，一个不落。"""
    gateway = Gateway()
    first = _FakeAdapter("onebot")
    second = _FakeAdapter("kook")
    gateway.register(first)
    gateway.register(second)

    await gateway.start()
    assert (first.started, second.started) == (1, 1)

    await gateway.stop()
    assert (first.stopped, second.stopped) == (1, 1)


async def test_gateway_publish_without_subscribers_is_quiet() -> None:
    """没订阅者：不抛、只是丢弃（debug 日志的事，行为上安静）。"""
    gateway = Gateway()
    await gateway.publish(PlatformEvent(platform="onebot", owner_id="u-admin"))


async def test_gateway_adapter_lookup() -> None:
    """按平台取适配器：注册了拿得到，没注册是 None。"""
    gateway = Gateway()
    adapter = _FakeAdapter("onebot")
    gateway.register(adapter)
    assert gateway.adapter("onebot") is adapter
    assert gateway.adapter("kook") is None
