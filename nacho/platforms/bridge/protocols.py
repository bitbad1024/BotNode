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

from .models import ActionResult, BotClient, ChatTarget


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

    async def reply(self, target: ChatTarget, content: str) -> ActionResult:
        """回复一条消息到 ``target`` 指向的会话（原样路径原样返回）。

        ``target`` 是**本平台自己产的回程地址**（生产与消费同平台，见
        :class:`~nacho.platforms.bridge.models.ChatTarget`）：适配器按自己 target 里的
        定位字段挑动作与参数键（OneBot 的 ``send_group_msg`` / ``send_private_msg``、
        Kook 的 ``send_channel_msg`` / ``send_dm_msg``），下游不再逐平台拼号。

        :raises ConnectionError: 环境问题当场抛（同 ``send`` 口径）。
        """
        ...

    def make_target(
        self,
        *,
        owner_id: str,
        chat: str = "other",
        chat_id: str = "",
        user_id: str = "",
        message_id: str = "",
    ) -> ChatTarget:
        """从**通用会话字段**构造本平台的回程地址（画布手动填的 target 节点用它）。

        与事件翻译那条路（适配器按平台事件自产 target）分工：这里是「没有事件、用户手动
        指一个会话」的场合 —— 画布上填平台 + 会话类型 + 会话号，适配器按自己的口径转
        （OneBot 号转整数、Kook 原样字符串），下游仍只认 ``ChatTarget`` 协议。

        :param owner_id: 这条会话定位属于谁（回复时按它挑在线连接）；
        :param chat: ``"group"`` / ``"private"`` / ``"other"``；
        :param chat_id: 会话号（群号 / 对方号）；
        :param user_id: 对方账号（私聊时兜底用，缺省回退到 ``chat_id``）；
        :param message_id: 消息号（撤回一类动作要用）。
        """
        ...
