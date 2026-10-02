"""Kook 机器人 Bot Token 的可逆加密：AES-256-GCM（``cryptography`` 的底层）。

为什么要可逆：Kook 是**正向 WS**（框架当客户端，主动连网关），连接时要把 Bot Token
明文拿去当 ``Authorization: Bot <token>`` 鉴权 —— 与 OneBot 不同（OneBot 是反向 WS，
框架当服务端、令牌是随机串，只存 sha256 摘要、按摘要查就行）。所以 Kook 的 Token
**落库要能解回来**，不能只存摘要。

方案：AES-GCM（认证加密），密钥 32 字节从配置给的 ``secret_key`` 派生 —— 新密文
（v2）用 **scrypt 带随机盐**派生：弱口令不能直接变成密钥，且每条密文盐不同、互不关联；
每条密文还自带**随机 nonce**，同一 Token 两次加密出来的密文也不同。GCM 的认证标签
保证密文没被篡改。

密文格式：``v1.<b64nonce>.<b64ciphertext+tag>``（早期，裸 sha256 派生、无盐，老库
仍能解）；``v2.<b64salt>.<b64nonce>.<b64ciphertext+tag>``（现在，scrypt 带盐派生）。
版本前缀留作将来换算法 / 轮换时识别。

依赖方向：本模块只依赖 ``botnode.core`` 的地位与标准库 + ``cryptography``，不 import 平台包、
不 import 接口层 —— 凭证层自己管「怎么把 Token 存成密文」，各平台只拿解出来的明文用。
"""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

#: 密文前缀版本号：v1 = 早期（sha256 派生、无盐），v2 = 现在（scrypt 带盐派生）
_PREFIX_V1: str = "v1"
_PREFIX_V2: str = "v2"
#: 每个密文独立的 nonce 长度（字节）；GCM 推荐 12
_NONCE_BYTES: int = 12
#: v2 的盐长度（字节）：随机盐让「同密钥、同明文」两次加密密文互不相同
_SALT_BYTES: int = 16
#: scrypt 参数：内存硬 + 时间成本，弱口令不能直接算出密钥
_SCRYPT_N: int = 2 ** 14
_SCRYPT_R: int = 8
_SCRYPT_P: int = 1


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text.encode("ascii"))


def derive_key(secret_key: str, *, salt: bytes = b"") -> bytes:
    """把配置给的 ``secret_key`` 派生为 32 字节的 AES-256 密钥。

    v2（新密文）用 scrypt 带随机盐派生：弱口令不会直接变成密钥，且每条密文盐不同、
    互不关联；``salt`` 留空（老 v1 密文）退回裸 sha256 拉平，保证老库还能解。
    """
    if not secret_key:
        raise ValueError("加密密钥不能为空：Kook Bot Token 落库需要 [kook].secret_key")
    if salt:
        return hashlib.scrypt(
            secret_key.encode("utf-8"),
            salt=salt,
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
            dklen=32,
        )
    return hashlib.sha256(secret_key.encode("utf-8")).digest()


def encrypt_token(token: str, secret_key: str) -> str:
    """把明文 Bot Token 加密成 ``v2.<salt>.<nonce>.<ciphertext+tag>``（原样落库）。

    随机盐 + scrypt 派生 + 随机 nonce：同 Token 两次加密出来密文也不同，弱口令也不
    直接等于密钥。
    """
    if not token:
        raise ValueError("要加密的 Token 不能为空")
    salt: bytes = os.urandom(_SALT_BYTES)
    key: bytes = derive_key(secret_key, salt=salt)
    nonce: bytes = os.urandom(_NONCE_BYTES)
    ciphertext: bytes = AESGCM(key).encrypt(nonce, token.encode("utf-8"), None)
    return ".".join([_PREFIX_V2, _b64(salt), _b64(nonce), _b64(ciphertext)])


def decrypt_token(encrypted: str, secret_key: str) -> str:
    """把密文解回明文 Token；密文坏 / 密钥不对 / 版本不认当场抛（不静默给空）。

    v1（老）与 v2（新）都能解：按前缀分支，v1 走裸 sha256、v2 走 scrypt + 盐。
    """
    parts: list[str] = encrypted.split(".")
    if parts[0] == _PREFIX_V2 and len(parts) == 4:
        salt: bytes = _unb64(parts[1])
        nonce: bytes = _unb64(parts[2])
        ciphertext: bytes = _unb64(parts[3])
        key: bytes = derive_key(secret_key, salt=salt)
    elif parts[0] == _PREFIX_V1 and len(parts) == 3:
        nonce = _unb64(parts[1])
        ciphertext = _unb64(parts[2])
        key = derive_key(secret_key)
    else:
        raise ValueError("Token 密文格式不认（版本或结构不符）")
    try:
        plaintext: bytes = AESGCM(key).decrypt(nonce, ciphertext, None)
    except Exception as exc:  # noqa: BLE001 — base64 坏串 / 认证失败统一按「解不开」抛
        raise ValueError("Token 密文解不开（密钥不对或密文被改动）") from exc
    return plaintext.decode("utf-8")
