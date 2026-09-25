"""接口层的业务层：只算「业务怎么办」，**不认识 FastAPI**。

一个模块一件事，模块自带全套（协议 + 默认实现 + 编排）::

    user/     用户：用户长什么样、账号密码规则、人到哪查、密码怎么落库 + 默认实现
    session/  会话：登录开出来的那一次会话（设备信息 + 令牌）、滑动续期、吊销
    auth/     鉴权：把「凭据换令牌」串起来的服务（令牌机制在 session 里）
    profile/  个人设置：改昵称 + 头像（头像存储走协议，换底层只换实现）

依赖方向是**单向**的：``auth`` 用 ``user`` 与 ``session``（登录得先查到人、再开会话），
``profile`` 用 ``user``（昵称在用户表里）；``user`` / ``session`` 谁都不认识上面两个。

业务层不 import :mod:`nacho.api.api`（入口层）—— 依赖只能从入口层指向这里，再由
:func:`nacho.api.create_app` 把实现装配起来。跨业务的（响应壳、错误出口、中间件、日志）
一律去 :mod:`nacho.api.common`。
"""
from __future__ import annotations

from .auth import AuthService, Credentials, CurrentUser, LoginResult
from .profile import (
    AvatarInfo,
    AvatarStore,
    FileAvatarStore,
    ProfileService,
    ProfileView,
)
from .session import ClientInfo, SessionRecord, SessionService

__all__ = [
    # 鉴权
    "AuthService",
    "Credentials",
    "CurrentUser",
    "LoginResult",
    # 会话
    "SessionService",
    "SessionRecord",
    "ClientInfo",
    # 个人设置
    "ProfileService",
    "ProfileView",
    "AvatarInfo",
    "AvatarStore",
    "FileAvatarStore",
]
