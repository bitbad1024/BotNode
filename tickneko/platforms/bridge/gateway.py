"""Gateway 总线：适配器的注册处、事件的分发处、发送的路由处。

它自己**不懂任何平台**——事件从适配器来（适配器翻译成
:class:`~tickneko.platforms.bridge.models.PlatformEvent` 后调 :meth:`Gateway.publish`），发送按
``platform`` 路由给对应适配器。两条口径：订阅者抛出的异常**只记日志**（事件分发是尽力
而为）；发送**环境问题当场抛**（没注册这个平台是装配问题，``ConnectionError`` 带上已
注册的平台列表），「发出去、对方答了不成功」才走 ``ActionResult`` 的 ``ok=False``。
装配形态见 ``docs/bridge/bridge.md``。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeAlias

from tickneko.core.logger import BaseLogger

from .logging import bridge_logger
from .models import ActionResult, ChatTarget, PlatformEvent
from .protocols import BotAdapter

#: 事件订阅者：收到一条规范化事件时的回调。抛出的异常只会被记下来（见模块文档）。
EventSubscriber: TypeAlias = Callable[[PlatformEvent], Awaitable[None]]


class Gateway:
    """平台无关的总线：注册适配器、分发事件、路由发送、管生命周期。"""

    def __init__(self, *, logger: BaseLogger | None = None) -> None:
        self._adapters: dict[str, BotAdapter] = {}
        self._subscribers: list[EventSubscriber] = []
        self._log: BaseLogger = logger if logger is not None else bridge_logger()

    # ------------------------------------------------------------------ 注册
    def register(self, adapter: BotAdapter) -> None:
        """注册一个适配器（按 ``adapter.platform`` 入索引）；平台重复当场 ``ValueError``。

        只登记、不启动 —— 生命周期归 :meth:`start` / :meth:`stop` 统一管，
        「先全部注册完再统一起」与 bootstrap 的装配顺序一致。
        """
        platform = adapter.platform
        if platform in self._adapters:
            raise ValueError(f"平台 {platform!r} 已注册：同一 Gateway 里平台标识不得重复")
        self._adapters[platform] = adapter
        self._log.info("适配器已注册", platform=platform)

    @property
    def platforms(self) -> tuple[str, ...]:
        """已注册的平台标识（注册顺序）。"""
        return tuple(self._adapters)

    def adapter(self, platform: str) -> BotAdapter | None:
        """按平台取适配器；没注册返回 ``None``。"""
        return self._adapters.get(platform)

    # ------------------------------------------------------------------ 订阅
    def subscribe(self, handler: EventSubscriber) -> None:
        """订阅规范化事件：每条事件按订阅顺序依次 ``await``。

        不提供退订 —— 订阅者是进程内的长期业务（workflow 触发、日志钩子），
        生命周期与进程同级，攒不下「订阅了又想退」的场景；真要过滤就订阅时自己判。
        """
        self._subscribers.append(handler)

    async def publish(self, event: PlatformEvent) -> None:
        """投递口：适配器翻译完事件后调它（构造时从 Gateway 拿走的那个 callable）。

        按订阅顺序逐个 ``await``；某个订阅者抛异常只记日志（platform / owner_id /
        堆栈），后面的订阅者照常收到 —— 事件分发尽力而为，不因一家失败整条停摆。
        """
        if not self._subscribers:
            self._log.debug(
                "事件无人订阅，已丢弃",
                platform=event.platform,
                owner_id=event.owner_id,
                kind=event.kind,
            )
            return
        for handler in self._subscribers:
            try:
                await handler(event)
            except Exception:  # noqa: BLE001 - 订阅者异常隔离，绝不影响总线
                self._log.exception(
                    "事件订阅者抛出异常",
                    platform=event.platform,
                    owner_id=event.owner_id,
                    kind=event.kind,
                )

    # ------------------------------------------------------------------ 发送
    async def send(
        self, platform: str, owner_id: str, action: str, /, **params: object
    ) -> ActionResult:
        """按平台路由发送：交给 ``platform`` 那个适配器发给 ``owner_id`` 的在线连接。

        :raises ConnectionError: 没注册这个平台（装配问题当场抛，消息里带上已注册
            平台，看得见、改得了）；归属下没有在线连接由适配器以同一口径抛。
        """
        adapter = self._adapters.get(platform)
        if adapter is None:
            known = "、".join(self._adapters) or "无"
            raise ConnectionError(f"没有 {platform!r} 平台的适配器（已注册：{known}）")
        return await adapter.send(owner_id, action, **params)

    async def reply(self, target: ChatTarget, content: str) -> ActionResult:
        """回复一条消息到 ``target`` 指向的会话：按 ``target.platform`` 路由回原适配器。

        事件自带会话定位（``PlatformEvent.target``，**产它那个适配器**的 target 类型），
        回复时**原样传回**即可 —— 发到哪、按什么动作发，由产 target 的适配器自己挑，
        调用方不碰平台字段。总线只认路由键，target 的形状它不管。

        :raises ConnectionError: 没注册这个平台（装配问题当场抛，消息里带上已注册
            平台）；归属下没有在线连接由适配器以同一口径抛。
        """
        adapter = self._adapters.get(target.platform)
        if adapter is None:
            known = "、".join(self._adapters) or "无"
            raise ConnectionError(f"没有 {target.platform!r} 平台的适配器（已注册：{known}）")
        return await adapter.reply(target, content)

    def make_target(
        self,
        platform: str,
        *,
        owner_id: str,
        chat: str = "other",
        chat_id: str = "",
        user_id: str = "",
        message_id: str = "",
    ) -> ChatTarget:
        """从通用会话字段构造某平台的回程地址（画布上手动构造会话定位的 ``pack`` 节点用它）。

        与 :meth:`reply` 同一条「按平台路由」的路：把字段交给 ``platform`` 那个适配器按它
        自己的口径转（OneBot 号转整数、Kook 原样字符串），调用方不碰平台字段。workflow 的
        ``pack`` 节点靠它（鸭子形状调 :attr:`~tickneko.workflow.nodes.base.NodeExecutionContext.
        gateway`，不 import bridge 类型）。

        :raises ConnectionError: 没注册这个平台（与 :meth:`send` / :meth:`reply` 同口径）。
        """
        adapter = self._adapters.get(platform)
        if adapter is None:
            known = "、".join(self._adapters) or "无"
            raise ConnectionError(f"没有 {platform!r} 平台的适配器（已注册：{known}）")
        return adapter.make_target(
            owner_id=owner_id,
            chat=chat,
            chat_id=chat_id,
            user_id=user_id,
            message_id=message_id,
        )

    # ------------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        """按注册顺序起所有适配器（幂等与否由各适配器自己保证，协议如此约定）。"""
        for adapter in self._adapters.values():
            await adapter.start()
        self._log.info("bridge 已启动", platforms=list(self._adapters))

    async def stop(self) -> None:
        """按注册的**逆序**停所有适配器：后起的先停，起的顺序倒着收。

        与 bootstrap 收尾同思路：后注册的往往依赖先注册的（未来 Kook 可能复用
        OneBot 的令牌设施），先停被依赖方容易踩到「设施还在被用就被拆了」。
        """
        for adapter in reversed(tuple(self._adapters.values())):
            await adapter.stop()
        self._log.info("bridge 已停止")
