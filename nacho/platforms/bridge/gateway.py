"""Gateway 总线：适配器的注册处、事件的分发处、发送的路由处。

它自己**不懂任何平台**——事件从适配器来（适配器翻译成
:class:`~nacho.platforms.bridge.models.PlatformEvent` 后调 :meth:`Gateway.publish`），发送按
``platform`` 路由给对应适配器。本模块只依赖 ``nacho.core``（logger）与同包的模型 /
协议，满足「bridge 不 import 平台包」的依赖方向。

装配形态（P2-5 的样子）::

    gateway = Gateway()
    adapter = OneBotAdapter(server, publish=gateway.publish)  # 投递口构造时交给适配器
    gateway.register(adapter)
    await gateway.start()          # -> adapter.start()

订阅与异常口径（对齐 onebot 的 ``_emit``）：handler 抛出的异常**只记日志**，不淹总线、
不影响其它订阅者 —— 事件分发是「尽力而为」，一条业务出错不该连累整条接入层。

发送口径（对齐 onebot 节点）：**环境问题当场抛**。没注册这个平台是装配问题，
:class:`ConnectionError` 带上当前已注册的平台列表，看得见、改得了；「发出去、对方答了
不成功」才走 :class:`~nacho.platforms.bridge.models.ActionResult` 的 ``ok=False``。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeAlias

from nacho.core.logger import BaseLogger, default_core

from .models import ActionResult, PlatformEvent
from .protocols import BotAdapter

#: 事件订阅者：收到一条规范化事件时的回调。抛出的异常只会被记下来（见模块文档）。
EventSubscriber: TypeAlias = Callable[[PlatformEvent], Awaitable[None]]


class Gateway:
    """平台无关的总线：注册适配器、分发事件、路由发送、管生命周期。"""

    def __init__(self, *, logger: BaseLogger | None = None) -> None:
        self._adapters: dict[str, BotAdapter] = {}
        self._subscribers: list[EventSubscriber] = []
        self._log: BaseLogger = logger if logger is not None else default_core().child("bridge")

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
