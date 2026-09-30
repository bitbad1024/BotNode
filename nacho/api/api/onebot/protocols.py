"""接口层需要的那点 OneBot 管理能力（**结构化协议**，不 import ``nacho.platforms.onebot``）。

为什么要绕一层协议：``nacho.platforms.onebot`` 依赖 ``websockets``，而 ``nacho.api`` 是独立的
可选依赖（``pip install "nacho[api]"`` 就能跑登录）。让接口层直接 import 它，等于装上
api 就必须装 onebot——所以这里只声明「接口层要用到哪些方法」，由
:class:`nacho.platforms.onebot.OneBotServer` / :class:`nacho.platforms.onebot.TokenRegistry`
**结构化满足**，两边不用互相 import（同一套路子见 :mod:`nacho.api.services.user.protocols`）。

数据那几个（:class:`ClientLike` / :class:`TokenLike`）的成员一律写成 **只读属性**
（``@property``）：对面是冻结数据类，字段不可写；协议里若声明成可写属性，
冻结数据类反而对不上（协议要求能赋值，它做不到）。

装配时主程序把服务挂到 ``app.state.onebot_server`` 上，路由按这里的协议取用。
"""
from __future__ import annotations

from typing import Protocol


class ClientLike(Protocol):
    """在线列表里的一行（对应 :class:`nacho.platforms.onebot.ClientEntry`）。"""

    @property
    def client_id(self) -> str:
        """这条连接自己的编号（踢人时按它定位）。"""
        ...

    @property
    def id(self) -> str:
        """这条连接属于谁（归属 owner_id；握手时由令牌定下来）。"""
        ...

    @property
    def bot_id(self) -> str:
        """这个机器人那一行的主键（多实例后与归属 owner_id 分开）。"""
        ...

    @property
    def account(self) -> str:
        """接入 WS 的那个 OneBot 机器人账号。"""
        ...

    @property
    def self_id(self) -> int | None:
        """机器人号；还没收到事件时是 ``None``。"""
        ...

    @property
    def remote(self) -> str:
        """对端地址。"""
        ...

    @property
    def connected_at(self) -> float:
        """连上的时刻（Unix 秒）。"""
        ...


class TokenLike(Protocol):
    """一条机器人凭证记录（对应 :class:`nacho.bots.BotCredential`，**不含明文**）。

    P5 泛化：``id`` 是**这一行机器人的主键（bot_id）**，归属另看 ``owner_id`` —— 一个
    用户可有多个机器人（多实例），身份从「owner_id 主键」升级为「(platform, bot_id)」。
    """

    @property
    def id(self) -> str:
        """记录主键（bot_id）：吊销 / 停用按它定位。"""
        ...

    @property
    def owner_id(self) -> str:
        """归属用户（谁的）：查昵称、判范围用它。"""
        ...

    @property
    def platform(self) -> str:
        """底层适配器（onebot / kook）。"""
        ...

    @property
    def account(self) -> str:
        """机器人账号（OneBot 是接入 WS 的机器人号；展示用）。"""
        ...

    @property
    def enabled(self) -> bool:
        """停用：记录还在，但不许再连。"""
        ...

    @property
    def remark(self) -> str:
        """备注。"""
        ...

    @property
    def created_at(self) -> float:
        """签发时间（Unix 秒）。"""
        ...


class IssuedLike(Protocol):
    """签发结果（对应 :class:`nacho.platforms.onebot.IssuedToken`）。"""

    @property
    def record(self) -> TokenLike:
        """令牌记录（不含明文）。"""
        ...

    @property
    def token(self) -> str:
        """明文令牌：只在这一次出现，之后查不回来。"""
        ...


class TokenRegistry(Protocol):
    """令牌注册表：把「连进来的令牌」翻成「哪条记录（含账号）」。

    服务端只认这一个协议，令牌存哪（库 / 内存 / 别处）由实现决定。

    这一份协议被接口层与 :mod:`nacho.platforms.onebot` **共用**：方法签名只用结构化类型
    :class:`TokenLike` / :class:`IssuedLike`，所以协议文件不 import ``nacho.platforms.onebot``
    ——接口层用得上，而 ``nacho.platforms.onebot`` 也不强制依赖 ``nacho.api``（运行时仅以
    :pep:`563` 惰性注解 + ``TYPE_CHECKING`` 引用本协议）。
    """

    async def resolve(self, token: str) -> TokenLike | None:
        """令牌对应的记录；令牌不存在 / 被吊销 / 停用就返回 ``None``（握手按 401 拒）。

        返回**整条记录**而不只是账号：连接上要记 ``id``，之后「删掉这个令牌」才知道
        该把哪些连接断开（见 :meth:`OneBotLike.revoke_by_id`）。
        """
        ...

    async def issue(
        self, owner_id: str, *, account: str = "", remark: str = "", platform: str = "onebot"
    ) -> IssuedLike:
        """给 ``owner_id``（谁的）签一个机器人；**多实例**，每次新 bot_id（不再换钥匙）。"""
        ...

    async def list_records(self, *, owner_id: str | None = None) -> tuple[TokenLike, ...]:
        """列出令牌（不含明文）；给 ``owner_id`` 就只列那一个归属的（过滤下推到实现里）。"""
        ...

    async def get_by_id(self, token_id: str) -> TokenLike | None:
        """按记录 id 取一条令牌；找不到返回 ``None``（不含明文）。"""
        ...

    async def remove_by_id(self, token_id: str) -> bool:
        """按记录 id 吊销；真删掉了返回 ``True``。"""
        ...

    async def set_enabled(self, token_id: str, enabled: bool) -> bool:
        """启用 / 停用一条令牌；真改到了返回 ``True``（id 不存在返回 ``False``）。"""
        ...


class OneBotLike(Protocol):
    """接口层要用的 OneBot 服务端（对应 :class:`nacho.platforms.onebot.OneBotServer`）。"""

    def roster(self, *, id: str | None = None) -> tuple[ClientLike, ...]:
        """在线客户端列表（快照）；给 ``id`` 就只看那一个归属（谁的）下的。"""
        ...

    async def kick(self, client_id: str, *, revoke: bool = False) -> bool:
        """踢掉一个客户端；``revoke=True`` 连令牌一起吊销。"""
        ...

    async def revoke_by_id(self, token_id: str) -> bool:
        """吊销一个令牌（按记录 id），并把正用它连着的客户端断开。"""
        ...

    async def set_token_enabled(self, token_id: str, enabled: bool) -> bool:
        """启用 / 停用一条令牌（记录还在）；停用会连同断开正用它连着的客户端。"""
        ...

    @property
    def tokens(self) -> TokenRegistry | None:
        """令牌注册表；``None`` 表示没配（不校验，也没有令牌可管）。"""
        ...
