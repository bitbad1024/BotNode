"""会话域：登录开出来的那一次会话（设备记录 + 令牌）与它们的吊销。

    models.py       ClientInfo / SessionRecord / IssuedSession / Authenticated
    client.py       从 ip、自报设备名、User-Agent 推出设备信息（纯函数）
    protocols.py    SessionStore：会话存储要什么能力
    store_sql.py    落库实现（auth_sessions 表，**主键就是令牌摘要**）
    tokens.py       令牌生成 + 「令牌摘要 -> 用户 id」的缓存映射
    service.py      编排：开 / 认 / 滑动续期 / 设备列表 / 双删吊销

**一种令牌**：明文进 HttpOnly Cookie，摘要既当表主键（列名 ``token_hash``，也就是对外那个
id）又当缓存键。缓存是"会话还活着"的凭据（闲置过期只活在这里），库里的行是**设备记录**
（列表与吊销）。详见 :mod:`nacho.api.services.session.service` 开头那段。
"""
from __future__ import annotations

from .client import describe_client, detect_browser, detect_device_type, detect_os
from .errors import TokenHashCollisionError
from .models import (
    DEVICE_DESKTOP,
    DEVICE_MOBILE,
    DEVICE_UNKNOWN,
    Authenticated,
    ClientInfo,
    IssuedSession,
    SessionRecord,
)
from .protocols import SessionStore
from .service import SessionService
from .store_sql import SessionTable, SqlSessionStore
from .tokens import TOKEN_PREFIX, TokenIndex, generate_token, hash_token

__all__ = [
    # 形状
    "ClientInfo",
    "SessionRecord",
    "IssuedSession",
    "Authenticated",
    "DEVICE_MOBILE",
    "DEVICE_DESKTOP",
    "DEVICE_UNKNOWN",
    # 设备信息解析
    "describe_client",
    "detect_browser",
    "detect_os",
    "detect_device_type",
    # 存储
    "SessionStore",
    "SqlSessionStore",
    "SessionTable",
    "TokenHashCollisionError",
    # 令牌
    "TokenIndex",
    "hash_token",
    "generate_token",
    "TOKEN_PREFIX",
    # 编排
    "SessionService",
]
