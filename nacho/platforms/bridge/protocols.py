"""适配器协议：一个平台接入器长什么样（**结构化协议**，不 import 任何平台包）。

为什么是 Protocol 而不是基类：同 :mod:`nacho.api.api.onebot.protocols` 的路数 ——
桥接层只声明「Gateway 需要哪些能力」，由各平台的适配器**结构化满足**；
``nacho.platforms.bridge`` 因此不 import ``nacho.platforms.onebot``（onebot 是可选依赖
``pip install "nacho[onebot]"``），写第二个适配器（Kook）也不必动这里。

方法口径（Gateway 依赖的就这几样）：

* ``platform`` —— 平台标识（事件路由 / 发送路由的键）；同一 Gateway 里不得重复；
* ``start`` / ``stop`` —— 生命周期（幂等，语义对齐
  :meth:`nacho.platforms.onebot.server.OneBotServer.start` / ``stop``）；
* ``clients()`` —— 在线列表快照（路由器「已连接设备」那张表）；
* ``send()`` —— 给某个归属的在线连接发一个动作并等回执。

适配器自己的事件怎么进 Gateway：构造时 Gateway 把投递口（``publish``）交给适配器
（见 :mod:`nacho.platforms.bridge.gateway`），适配器收到平台事件、翻译成
:class:`~nacho.platforms.bridge.models.PlatformEvent` 后调它 —— 协议里不体现这一面，
那是构造约定，不是能力约定。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import ActionResult, BotClient, EventTarget


@runtime_checkable
class BotAdapter(Protocol):
    """一个平台适配器：连接管理 + 事件翻译 + 能力转述。"""

    @property
    def platform(self) -> str:
        """平台标识（小写、稳定，如 ``"onebot"`` / ``"kook"``）；路由键。"""
        ...

    async def start(self) -> None:
        """开始监听（幂等：已在监听就什么都不做）。"""
        ...

    async def stop(self) -> None:
        """停止：关监听并断开所有客户端（幂等）。"""
        ...

    def clients(self, *, owner_id: str | None = None) -> tuple[BotClient, ...]:
        """在线列表快照；给 ``owner_id`` 就只看那个归属下的。

        快照口径与 :meth:`OneBotServer.roster <nacho.platforms.onebot.server.OneBotServer.roster>`
        一致：取那一刻的样子，不代表此刻还连着。
        """
        ...

    async def send(self, owner_id: str, action: str, /, **params: object) -> ActionResult:
        """给 ``owner_id`` 的在线连接发一个动作并等回执（ActionResponse 的规范化版）。

        :raises ConnectionError: 环境问题当场抛（没这个归属的在线连接）——与 onebot
            节点「没配好看得见」同一口径；走到 :class:`~nacho.platforms.bridge.models.ActionResult`
            里的失败是「发出去、对方答了不成功」，两者分开。
        """
        ...

    async def reply(self, target: EventTarget, content: str) -> ActionResult:
        """回复一条消息到 ``target`` 指向的会话（原样路径原样返回）。

        适配器按 ``target.chat`` 挑动作与参数键（OneBot 的 ``send_group_msg`` /
        ``send_private_msg``、Kook 的 ``send_channel_msg`` / ``send_dm_msg``），
        下游（工作流 / 接口层）不再逐平台拼 ``group_id`` / ``user_id``。

        :raises ConnectionError: 环境问题当场抛（同 ``send`` 口径）。
        """
        ...
