"""桥接层测试：规范化模型（P2-1）、协议（P2-2）、Gateway（P2-3）、OneBot 适配器（P2-4）。

这一份先覆盖**模型口径**——身份字段统一字符串、`raw` 兜底、冻结不可改；后续小节
随实现逐段长出来。
"""
from __future__ import annotations

import dataclasses

import pytest

from nacho.bridge.models import ActionResult, BotClient, PlatformEvent


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
