"""通用「机器人凭证」模型：一个用户添加一个机器人 = 一行凭证（跨平台）。

与旧 :mod:`tickneko.platforms.onebot.tokens` 的区别：旧模型是**一个归属一条令牌**（``id`` 即
owner_id 主键，再签 = 换钥匙），只服务 OneBot 反向 WS 的多归属场景；这里是**一个机器人
一行**（``bot_id`` 主键，一个用户可拥有多个机器人），``platform`` 区分底层适配器
（onebot / kook）。

字段三组看：

* 身份 —— ``platform``（底层适配器，onebot / kook）/ ``owner_id``（归属用户）/ ``bot_id``
  （这一行的主键，``(platform, bot_id)`` 跨平台唯一）；
* 凭证 —— ``token_hash``（令牌 / Bot Token 的 sha256 摘要；**不存明文**）。OneBot 反向 WS
  握手按它认连接，Kook 正向连接所需的明文 Bot Token 在 P6 另作可逆加密，不进这张表的摘要列；
* 展示与开关 —— ``account`` / ``remark`` / ``enabled`` / ``created_at``。

本模块是纯数据模型（冻结），不 import 任何平台包，只依赖 ``tickneko.core`` 的地位（与
:mod:`tickneko.platforms.onebot.models` 同级）。存储见 :mod:`tickneko.bots.store`。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

#: 底层适配器（平台）标识：onebot（反向 WS）/ kook（正向 WS）；后续平台在这里加
BotPlatform: TypeAlias = Literal["onebot", "kook"]


@dataclass(frozen=True)
class BotCredential:
    """一条机器人凭证记录（**不含明文 token**，可以放心列给管理接口看）。

    :param platform: 底层适配器（onebot / kook）；
    :param owner_id: 归属用户 id（谁添加的这个机器人）；
    :param bot_id: 这一行的主键（跨平台唯一：``(platform, bot_id)``）；旧 OneBot 里它
        曾是 owner_id 本身，泛化后独立成行 id；
    :param token_hash: 令牌 / Bot Token 的 sha256 摘要（明文不落库；握手按它查唯一索引）；
    :param token_secret: **Kook 专用**：Bot Token 的 AES-GCM 密文（``v1.<nonce>.<密文>``）。
        Kook 是正向 WS，连接时要拿明文 Token 鉴权，所以得**可逆**存一份密文（解回来用）；
        OneBot 是随机令牌 + 反向 WS，只存摘要、无此列值。**这个字段不进管理接口响应**。
    :param account: 机器人账号（OneBot 是接入 WS 的机器人号，Kook 是 Bot 名；展示用）；
    :param enabled: 停用开关（记录还在，但不许再连 / 再启）；
    :param remark: 备注；
    :param created_at: 创建时间（Unix 秒）。
    """

    platform: BotPlatform
    owner_id: str
    bot_id: str
    token_hash: str = ""
    token_secret: str = ""
    account: str = ""
    enabled: bool = True
    remark: str = ""
    created_at: float = 0.0

    @property
    def id(self) -> str:
        """记录主键（= bot_id）：兼容旧 ``TokenLike.id`` 的主键语义，但归属另看 ``owner_id``。"""
        return self.bot_id
