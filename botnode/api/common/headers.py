"""接口层对外的几个自定义响应头，以及配套的 ``request.state`` 键名。

放这一处纯粹是因为它们是**跨模块的约定**：鉴权依赖负责算出会话还剩多少秒，请求日志中间件
负责把它写进响应头——两边都得知道同一个名字，各写一遍字符串迟早对不上。

    SESSION_EXPIRES_HEADER   ``X-Session-Expires-In``（鉴权 -> 响应头）
    SESSION_EXPIRES_STATE    鉴权挂在 ``request.state`` 上的属性名（中间件按它取）

注意 ``X-Trace-Id`` **不在这里**：那个是请求编号，本来就是日志的概念，常量留在
:mod:`botnode.api.logging`（写它的也是请求日志中间件）。
"""
from __future__ import annotations

#: 会话剩余有效期的响应头（秒）：令牌每次请求都会**滑动续期**，前端倒计时跟着它走，
#: 不然倒计时会一直按登录那一刻算、越走越偏。``0`` 表示不过期。
SESSION_EXPIRES_HEADER: str = "X-Session-Expires-In"
#: 剩余秒数挂在这个 ``request.state`` 属性上：鉴权依赖写，中间件读。
SESSION_EXPIRES_STATE: str = "session_expires_in"
