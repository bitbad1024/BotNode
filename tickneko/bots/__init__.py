"""通用「机器人凭证」层：一个用户添加一个机器人 = 一行凭证，跨平台。

解决什么：旧 :mod:`tickneko.platforms.onebot.tokens` 的「一个归属一条令牌」只服务 OneBot 反向 WS；
接入 Kook 后「机器人」是跨平台概念（一个用户可有多个机器人），需要一张带 ``platform``
的通用凭证表。这里放：

* ``models.py``  :class:`BotCredential`：platform / owner_id / bot_id / token_hash …
* ``store.py``   :class:`SqlBotStore`：查 ``bot_credentials`` 表（bot_id 主键，多实例）

依赖方向：本包只依赖 ``tickneko.core`` 的地位，**不** import ``tickneko.platforms.onebot`` /
``tickneko.platforms.kook`` / ``tickneko.api`` —— 平台差异由 ``platform`` 字段区分，凭证的
「怎么连 / 怎么发」留给各平台包，这里只管「这行凭证是谁的、存没存对」。

与旧 ``onebot_tokens`` 的关系：新表 ``bot_credentials``（``bot_id`` 主键 + ``platform`` 列）
取代它；旧表由运维手动删除（不写迁移）。
"""
from __future__ import annotations

from .crypto import decrypt_token, derive_key, encrypt_token
from .models import BotCredential, BotPlatform
from .store import BotCredentialTable, IssuedBotCredential, SqlBotStore, hash_token

__all__ = [
    "BotCredential",
    "BotPlatform",
    "BotCredentialTable",
    "IssuedBotCredential",
    "SqlBotStore",
    "hash_token",
    "derive_key",
    "encrypt_token",
    "decrypt_token",
]
