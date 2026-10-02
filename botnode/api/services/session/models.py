"""会话域的对象（**不认识 HTTP**）。

三个形状::

    ClientInfo     这次登录是从哪来的：ip、设备名、手机还是电脑、浏览器、系统
    SessionRecord  库里的样子：一次登录一行，主键就是**令牌摘要**
    IssuedSession  开一次会话交出去的东西：令牌（明文）+ 有效期

**只有一种令牌**：它是随机串，摘要既当表主键（表列就叫 ``token_hash``）又当缓存键；
勾「记住设备」只是把滑动有效期设长、并让 Cookie 带上 ``Max-Age``（持久 Cookie），
没有第二种令牌。

对外一律叫 ``token_hash`` 而**不叫 session_id**：它俩本来就是同一个值（令牌摘要），
起两个名字只会让人以为是两样东西。见 :mod:`botnode.api.services.session.tokens` 开头那段。
"""
from __future__ import annotations

from dataclasses import dataclass

#: 设备类型：手机 / 电脑 / 认不出来
DEVICE_MOBILE: str = "mobile"
DEVICE_DESKTOP: str = "desktop"
DEVICE_UNKNOWN: str = "unknown"


@dataclass(frozen=True)
class ClientInfo:
    """这次登录来自哪个客户端（登录时记下来，供「登录设备」列表展示）。"""

    #: 客户端 ip（默认取连接的来源地址；配了信任代理才看 X-Forwarded-For）
    ip: str = ""
    #: 原始 User-Agent（留着排障）
    user_agent: str = ""
    #: 设备名：优先客户端自报（``X-Device-Name``），没有就从 UA 推一个
    device_name: str = ""
    #: 手机还是电脑：``mobile`` / ``desktop`` / ``unknown``
    device_type: str = DEVICE_UNKNOWN
    #: 浏览器：Chrome / Safari / Firefox / Edge / …
    browser: str = ""
    #: 操作系统：Windows / macOS / iOS / Android / Linux
    os: str = ""


@dataclass(frozen=True)
class SessionRecord:
    """一次登录在库里的样子（对应 ``auth_sessions`` 表的一行）。"""

    #: **令牌摘要**（不是明文）：表主键，也是对外露的那个 id
    token_hash: str
    user_id: str
    created_at: float
    #: 是否勾了「记住设备」：勾了滑动有效期长、Cookie 也持久
    remembered: bool = False
    ip: str = ""
    device_name: str = ""
    device_type: str = DEVICE_UNKNOWN
    browser: str = ""
    os: str = ""
    user_agent: str = ""


@dataclass(frozen=True)
class IssuedSession:
    """开一次会话交出去的东西。"""

    session: SessionRecord
    #: 令牌**明文**：只在这一刻出现（客户端拿去存 Cookie）
    token: str
    #: 还有多少秒过期；``0`` 表示不过期（前端据此决定要不要显示倒计时）
    expires_in: int = 0


@dataclass(frozen=True)
class Authenticated:
    """拿着令牌认出来的东西：是谁、在哪条会话上、这次续期到什么时候。"""

    user_id: str
    #: 认出来的是哪条会话 = **令牌摘要**（对外它就是这条会话的 id）
    token_hash: str
    #: 滑动续期后的剩余秒数（填响应头，前端倒计时跟着它走）；``0`` 表示不过期
    expires_in: int = 0
