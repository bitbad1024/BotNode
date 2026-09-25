"""个人设置入口层：``<prefix>/profile`` 一组接口（改昵称 / 头像）。

    requests.py     进来的请求体：UpdateProfileRequest（改昵称）
    responses.py    出去的响应体：ProfileData（资料 + 头像信息）
    dependencies.py 路由的注入件：怎么拿到业务层的服务
    router.py       HTTP 入口：``GET/PATCH <prefix>/profile``、
                    ``PUT/GET/DELETE <prefix>/profile/avatar``、``GET .../avatar/{user_id}``

头像的**字节流**不套 JSON 壳：``PUT`` 的请求体就是图片本身，``GET`` 回的就是图片本身
（带 ``ETag`` / ``Last-Modified``，浏览器会拿 ``If-None-Match`` 回来换 304）；其余照旧走
``ApiResponse[ProfileData]``。业务怎么算（大小 / 类型 / 落哪）在
:class:`nacho.api.services.profile.ProfileService`。
"""
from __future__ import annotations

from .dependencies import ProfileServiceDep, get_profile_service
from .requests import UpdateProfileRequest
from .responses import ProfileData
from .router import router

__all__ = [
    "ProfileData",
    "ProfileServiceDep",
    "UpdateProfileRequest",
    "get_profile_service",
    "router",
]
