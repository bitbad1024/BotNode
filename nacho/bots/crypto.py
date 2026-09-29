"""Kook 机器人 Bot Token 的可逆加密：AES-256-GCM（``cryptography`` 的底层）。

为什么要可逆：Kook 是**正向 WS**（框架当客户端，主动连网关），连接时要把 Bot Token
明文拿去当 ``Authorization: Bot <token>`` 鉴权 —— 与 OneBot 不同（OneBot 是反向 WS，
框架当服务端、令牌是随机串，只存 sha256 摘要、按摘要查就行）。所以 Kook 的 Token
**落库要能解回来**，不能只存摘要。

方案：AES-GCM（认证加密），密钥 32 字节从配置给的 ``secret_key`` 派生（sha256 拉平到
正好 32 字节），每条密文自带**随机 nonce** —— 同一 Token 两次加密出来的密文也不同，
且 GCM 的认证标签保证密文没被篡改。

密文格式 ``v1.<b64nonce>.<b64ciphertext+tag>``（``v1`` 留版本号，将来换算法能识别）。

依赖方向：本模块只依赖 ``nacho.core`` 的地位与标准库 + ``cryptography``，不 import 平台包、
不 import 接口层 —— 凭证层自己管「怎么把 Token 存成密文」，各平台只拿解出来的明文用。
"""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

#: 密文前缀版本号：将来换算法 / 改密钥派生时，靠它认旧密文
_PREFIX: str = "v1"
#: 每个密文独立的 nonce 长度（字节）；GCM 推荐 12
_NONCE_BYTES: int = 12


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text.encode("ascii"))


def derive_key(secret_key: str) -> bytes:
    """把配置给的 ``secret_key`` 派生为 32 字节的 AES-256 密钥（sha256 拉平）。"""
    if not secret_key:
        raise ValueError("加密密钥不能为空：Kook Bot Token 落库需要 [kook].secret_key")
    return hashlib.sha256(secret_key.encode("utf-8")).digest()


def encrypt_token(token: str, secret_key: str) -> str:
    """把明文 Bot Token 加密成 ``v1.<nonce>.<ciphertext+tag>`` 这样的串（原样落库）。"""
    if not token:
        raise ValueError("要加密的 Token 不能为空")
    key: bytes = derive_key(secret_key)
    nonce: bytes = os.urandom(_NONCE_BYTES)
    ciphertext: bytes = AESGCM(key).encrypt(nonce, token.encode("utf-8"), None)
    return ".".join([_PREFIX, _b64(nonce), _b64(ciphertext)])


def decrypt_token(encrypted: str, secret_key: str) -> str:
    """把密文解回明文 Token；密文坏 / 密钥不对 / 版本不认当场抛（不静默给空）。"""
    parts: list[str] = encrypted.split(".")
    if len(parts) != 3 or parts[0] != _PREFIX:
        raise ValueError("Token 密文格式不认（版本或结构不符）")
    key: bytes = derive_key(secret_key)
    try:
        nonce: bytes = _unb64(parts[1])
        ciphertext: bytes = _unb64(parts[2])
        plaintext: bytes = AESGCM(key).decrypt(nonce, ciphertext, None)
    except Exception as exc:  # noqa: BLE001 — base64 坏串 / 认证失败统一按「解不开」抛
        raise ValueError("Token 密文解不开（密钥不对或密文被改动）") from exc
    return plaintext.decode("utf-8")
