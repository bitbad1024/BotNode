"""哈希串与令牌里那点 base64 编解码（两个业务模块共用）。

密码哈希串（盐、摘要）与令牌（载荷、签名）都要把二进制写成能安全放在文本里的样子：
URL 安全的 base64，并去掉尾部 ``=``（放进令牌里更好看）。
"""
from __future__ import annotations

import base64


def b64(data: bytes) -> str:
    """URL 安全的 base64，去掉尾部 ``=``（令牌里带着不好看）。"""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def unb64(text: str) -> bytes:
    """:func:`b64` 的逆操作；补回被去掉的 ``=``。"""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
