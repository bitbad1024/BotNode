"""错误码与错误相关的状态码常量。

单独一份是为了让「有哪些错误」一眼看完，也方便客户端拿它做分支 ——
码本身是稳定契约：改文案可以，改码要当破坏性变更。

错误码是**全局**的（不按业务模块分）：客户端拿它做分支时不想关心「这条是哪块业务抛的」，
新增业务模块往这里加码即可。
"""
from __future__ import annotations

from enum import IntEnum, StrEnum


class ErrorCode(StrEnum):
    """错误码：给客户端做分支用，稳定不变（改文案可以，改码要当破坏性变更）。"""

    #: 请求没过 pydantic（缺字段、格式不对）
    VALIDATION_ERROR = "VALIDATION_ERROR"
    #: 账号或密码不对（账号存不存在不区分，统一这个码）
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    #: 账号被停用
    ACCOUNT_DISABLED = "ACCOUNT_DISABLED"
    #: 注册时这个账号已被占用（账号唯一，同账号只能注册一次）
    ACCOUNT_ALREADY_EXISTS = "ACCOUNT_ALREADY_EXISTS"
    #: 上传的头像超过大小上限
    AVATAR_TOO_LARGE = "AVATAR_TOO_LARGE"
    #: 上传的不是认得的图片类型（按文件头认，SVG 这类 XML 不收）
    AVATAR_TYPE_UNSUPPORTED = "AVATAR_TYPE_UNSUPPORTED"
    #: 没带令牌 / 令牌用不了
    UNAUTHORIZED = "UNAUTHORIZED"
    #: 令牌被改过 / 不是本服务签的
    TOKEN_INVALID = "TOKEN_INVALID"
    #: 令牌过期了，重新登录
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    #: 路由不存在等 HTTP 层错误
    HTTP_ERROR = "HTTP_ERROR"
    #: 兜底：服务端出错了（日志里有堆栈，响应里没有）
    INTERNAL_ERROR = "INTERNAL_ERROR"


class HttpStatus(IntEnum):
    """HTTP 状态码（与 :class:`ErrorCode` 平级的传输层常量）。

    单独成枚举，便于「有哪些状态码」一眼看完、统一维护；也避开 starlette 把
    ``UNPROCESSABLE_CONTENT`` 改名带来的弃用警告——直接引用枚举成员，不散落字面量。
    """

    #: 422：请求体没过校验（pydantic 校验失败）
    UNPROCESSABLE_ENTITY = 422
    #: 413：请求体太大（上传的头像超了上限）
    REQUEST_ENTITY_TOO_LARGE = 413
    #: 415：内容类型不支持（上传的不是认得的图片）
    UNSUPPORTED_MEDIA_TYPE = 415