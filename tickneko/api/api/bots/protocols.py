"""机器人管理的服务协议：跨平台「增 / 启停 / 删」+ 在线聚合（**结构化协议**）。

为什么再绕一层协议：机器人管理跨平台（OneBot 反向 WS / Kook 正向 WS），而「Kook 的
增」要起正向 WS 客户端、「Kook 的删」要停客户端——这些是平台包（``tickneko.platforms.kook`` /
``tickneko.platforms.bridge.kook``）的活，接口层（``tickneko.api``，可独立 ``pip install
"tickneko[api]"``）不能 import 它们。所以接口层只声明「机器人管理要用到哪些能力」，由
装配层注入的实现（:class:`tickneko.platforms.bridge.manager.BotManager`）**结构化满足**——
同一套路子见 :mod:`tickneko.api.api.onebot.protocols`。

记录形状复用 :class:`~tickneko.api.api.onebot.protocols.TokenLike` /
:class:`~tickneko.api.api.onebot.protocols.IssuedLike`：一条凭证「不含明文」是跨平台的，
Kook 凭证同样满足（``id`` = bot_id、``owner_id`` = 归属、``platform`` = 底层适配器）。
明文（OneBot 随机令牌 / Kook 用户自填的 Bot Token）只在签发那一次出现。
"""
from __future__ import annotations

from typing import Protocol

from ..onebot.protocols import IssuedLike, TokenLike


class OnlineBot(Protocol):
    """在线客户端快照里的**那一格**（跨平台归一口径，对应 :class:`tickneko.platforms.bridge.models.BotClient`）。

    ``self_id`` 统一是字符串（OneBot 的整数号在适配器里已转字符串；Kook 本来就是字符串），
    没学到机器人号时是空串。
    """

    @property
    def client_id(self) -> str:
        """这条连接 / 这个机器人自己的编号（展示或定位用）。"""
        ...

    @property
    def self_id(self) -> str:
        """机器人号（字符串口径）；还没收到事件时是空串。"""
        ...

    @property
    def remote(self) -> str:
        """对端地址（Kook 正向 WS 没有，空串）。"""
        ...

    @property
    def connected_at(self) -> float:
        """连上的时刻（Unix 秒）；没连过是 0。"""
        ...


class BotsService(Protocol):
    """机器人管理服务：把凭证落库 + 平台适配器生命周期统一成一套操作。

    接口层只认这一个协议；具体实现（BotManager）在装配层注入，平台差异被封在实现里：
    接口层不知道「Kook 的启停要 start / stop 正向 WS 客户端」这种细节。
    """

    async def list_records(self, *, owner_id: str | None = None) -> tuple[TokenLike, ...]:
        """列机器人（多实例：一个用户多条）；给 ``owner_id`` 就只列那个归属下的。"""
        ...

    async def get_by_id(self, bot_id: str) -> TokenLike | None:
        """按记录主键（bot_id）取一条；找不到返回 ``None``（不含明文）。"""
        ...

    async def issue(
        self,
        owner_id: str,
        *,
        platform: str = "onebot",
        account: str = "",
        remark: str = "",
        token: str | None = None,
    ) -> IssuedLike:
        """给 ``owner_id`` 签一个机器人；``token`` 是 Kook 用户自填的 Bot Token（OneBot 传 None 自动生成）。

        :raises ValueError: Kook 没给 token / 没配加密密钥等非法输入。
        """
        ...

    async def set_enabled(self, bot_id: str, enabled: bool) -> bool:
        """启用 / 停用一个机器人；真改到了返回 ``True``。

        停用会同步断开 / 停掉连着它的客户端（Kook 是停正向 WS 客户端），启用则重新拉起。
        """
        ...

    async def remove_by_id(self, bot_id: str) -> bool:
        """删除一个机器人（吊销凭证并断开 / 停掉客户端）；真删掉了返回 ``True``。"""
        ...

    def online_clients(self, record: TokenLike) -> tuple[OnlineBot, ...]:
        """这一个机器人此刻挂在线的客户端快照（派生态，不落库）；没在线是空。"""
        ...