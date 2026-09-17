"""用户模块意义上的「密码怎么落库」的默认实现：PBKDF2-SHA256（标准库 ``hashlib``）。

串里自带算法、迭代次数、盐与摘要（``算法$迭代$盐$摘要``），所以：

* 换迭代次数不影响老密码登录（老串里记着它自己那份参数）；
* 每个密码一份新盐，同一密码两份串也不一样；
* 比对用 ``hmac.compare_digest`` —— 定长时间比较，不泄露「前面几位已经对了」。

这份是 :class:`~nacho.api.services.user.protocols.PasswordHasher` 的默认实现（不引第三方，
不接数据库也能跑）；换成 argon2 / bcrypt 之类只要方法签名一致即可替换。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Final

from ...common.encoding import b64, unb64

#: 摘要算法（PBKDF2 的 HMAC 部分用这一个）
_HASH_NAME: Final[str] = "sha256"


class Pbkdf2PasswordHasher:
    """PBKDF2-SHA256 密码哈希，串自带盐与迭代次数。"""

    _ALGORITHM: Final[str] = "pbkdf2_sha256"

    def __init__(self, *, iterations: int = 200_000, salt_bytes: int = 16) -> None:
        """
        :param iterations: 迭代次数（越大越慢越抗暴力，200k 约几十毫秒一次）；
        :param salt_bytes: 盐的字节数，每个密码一份新盐。
        """
        self._iterations: int = iterations
        self._salt_bytes: int = salt_bytes

    def hash(self, password: str) -> str:
        """把明文密码变成 ``算法$迭代$盐$摘要`` 这样的串（原样落库即可）。"""
        salt: bytes = secrets.token_bytes(self._salt_bytes)
        digest: bytes = hashlib.pbkdf2_hmac(
            _HASH_NAME, password.encode("utf-8"), salt, self._iterations
        )
        return "$".join([self._ALGORITHM, str(self._iterations), b64(salt), b64(digest)])

    def verify(self, password: str, hashed: str) -> bool:
        """比对明文与库里那份哈希；哈希串坏了 / 算法不认就返回 ``False``（不抛）。"""
        parts: list[str] = hashed.split("$")
        if len(parts) != 4 or parts[0] != self._ALGORITHM:
            return False
        try:
            iterations = int(parts[1])
            salt = unb64(parts[2])
            expected = unb64(parts[3])
        except ValueError:  # base64 坏串 / 迭代次数不是整数
            return False
        digest: bytes = hashlib.pbkdf2_hmac(
            _HASH_NAME, password.encode("utf-8"), salt, iterations
        )
        return hmac.compare_digest(digest, expected)
