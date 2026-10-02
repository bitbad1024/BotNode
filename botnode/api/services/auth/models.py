"""鉴权业务层的输入 / 输出对象（**不认识 HTTP**）。

入口层把请求体翻译成 :class:`Credentials` 再交给
:class:`~botnode.api.services.auth.service.AuthService`；服务返回 :class:`LoginResult` /
:class:`CurrentUser`，入口层再装配成对外的响应模型 —— 这样业务层不会反过来依赖 HTTP
schema。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..session.models import SessionRecord
from ..user.models import UserProfile


@dataclass(frozen=True)
class Credentials:
    """登录凭据（业务层的输入）。

    ``password`` 是**明文**，只在这一次调用里短暂存在；``repr`` 里被隐去，避免误打印。
    """

    account: str
    password: str = field(repr=False)


@dataclass(frozen=True)
class LoginResult:
    """登录成功的结果：令牌 + 有效期 + 用户资料。"""

    token: str
    #: 令牌还有多少秒过期（会随滑动续期往后延）；``0`` 表示不过期
    expires_in: int
    user: UserProfile
    #: 这次登录开出来的会话（设备信息在里面；``token_hash`` 就是令牌摘要）
    session: SessionRecord | None = None
    #: 这次登录是**复用**了客户端手里那个旧令牌（没新建会话），还是新发了一个
    reused: bool = False


@dataclass(frozen=True)
class CurrentUser:
    """用访问令牌认出来的当前用户。"""

    user: UserProfile
    #: 认出来的是哪条会话 = **令牌摘要**（「登录设备」列表要把当前这条标出来 / 排除自己）
    token_hash: str = ""
    #: 滑动续期后的剩余秒数（入口层把它写进响应头，前端倒计时跟着走）；``0`` = 不过期
    expires_in: int = 0
