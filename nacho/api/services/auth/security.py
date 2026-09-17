"""鉴权模块的默认实现：HMAC 签名的不透明令牌。

令牌形状是 ``nacho1.<base64url(载荷)>.<签名>``，签名是 ``HMAC-SHA256(密钥, 载荷)``：

* **不透明**（不是 JWT）：不带任何权限信息，服务端只认签名与过期时间，想知道更多就去查
  用户存储；要带更多信息往 :class:`~nacho.api.services.auth.protocols.TokenClaims` 里加字段；
* 校验顺序是**先看形状、再比签名、最后解内容**：签名对不上就不碰载荷。

:func:`resolve_secret` 管「没配密钥怎么办」：现生成一个随机的能用，但重启即失效，正式
环境必须在 ``[api].secret`` 里写死一份。
"""
from __future__ import annotations

import hmac
import json
import secrets
import time
from typing import Final, cast

from ...common.encoding import b64, unb64
from ...common.errors import TokenExpiredError, TokenInvalidError
from .protocols import TokenClaims

#: 摘要算法（HMAC 用这一个）
_HASH_NAME: Final[str] = "sha256"
#: 令牌前缀：换了格式就换前缀，老令牌直接不认（而不是解析一半报错）
TOKEN_PREFIX: Final[str] = "nacho1"


def resolve_secret(secret: str = "") -> str:
    """定下签名密钥：配了就用配置里的，没配现生成一个随机的。

    随机密钥只在内存里，重启即变 —— 已签发的令牌随之失效，所以正式环境要配 ``secret``。
    """
    return secret or secrets.token_urlsafe(32)


class HmacTokenService:
    """HMAC 签名的不透明令牌：``nacho1.<载荷>.<签名>``。"""

    def __init__(self, secret: str, *, ttl: float = 3600.0) -> None:
        """
        :param secret: 签名密钥（配置里的 ``api.secret``，空串请先过 :func:`resolve_secret`）；
        :param ttl: 默认有效期（秒），签发时可以按次覆盖。
        :raises ValueError: 密钥是空的。
        """
        if not secret:
            raise ValueError("令牌密钥不能为空；不配置请用 resolve_secret() 现生成一个")
        self._secret: bytes = secret.encode("utf-8")
        self._ttl: float = ttl

    @property
    def ttl(self) -> float:
        """默认有效期（秒）。"""
        return self._ttl

    def issue(self, subject: str, *, ttl: float | None = None) -> str:
        """给 ``subject``（用户 id）签一个令牌，默认用 :attr:`ttl` 秒后过期。"""
        now: float = time.time()
        payload: dict[str, object] = {
            "sub": subject,
            "iat": now,
            "exp": now + (self._ttl if ttl is None else ttl),
            "jti": secrets.token_urlsafe(8),
        }
        body: str = b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        return f"{TOKEN_PREFIX}.{body}.{self._sign(body)}"

    def parse(self, token: str) -> TokenClaims:
        """解析并校验令牌；过期抛 :class:`TokenExpiredError` / 别的都 :class:`TokenInvalidError`。

        顺序是先看形状、再比签名、最后解内容：签名不对就不碰载荷。
        """
        parts: list[str] = token.split(".")
        if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
            raise TokenInvalidError()
        body, signature = parts[1], parts[2]
        if not hmac.compare_digest(self._sign(body), signature):
            raise TokenInvalidError()
        try:
            raw = cast("object", json.loads(unb64(body)))
        except (ValueError, UnicodeDecodeError) as exc:  # 坏 base64 / 不是 JSON
            raise TokenInvalidError() from exc
        if not isinstance(raw, dict):
            raise TokenInvalidError()
        payload = cast("dict[str, object]", raw)
        sub = payload["sub"]
        iat = payload["iat"]
        exp = payload["exp"]
        if (
            not isinstance(sub, str)
            or not isinstance(iat, (int, float))
            or not isinstance(exp, (int, float))
        ):
            raise TokenInvalidError()
        try:
            claims = TokenClaims(
                subject=str(sub),
                issued_at=float(iat),
                expires_at=float(exp),
                token_id=str(payload.get("jti", "")),
            )
        except (KeyError, ValueError) as exc:  # 缺字段 / 类型不对（兜底）
            raise TokenInvalidError() from exc
        if claims.is_expired():
            raise TokenExpiredError()
        return claims

    def _sign(self, body: str) -> str:
        """给载荷算签名（HMAC-SHA256，结果再 base64url）。"""
        digest: bytes = hmac.new(self._secret, body.encode("ascii"), _HASH_NAME).digest()
        return b64(digest)
