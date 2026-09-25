"""个人设置的 HTTP 入口：改昵称 + 头像（传 / 取 / 删）。

    GET    <prefix>/profile                    我的资料 + 头像信息
    PATCH  <prefix>/profile                    改昵称
    PUT    <prefix>/profile/avatar             上传头像（请求体就是图片字节）
    GET    <prefix>/profile/avatar             我的头像（图片字节）
    DELETE <prefix>/profile/avatar             删掉头像
    GET    <prefix>/profile/avatar/{user_id}   看某人的头像（登录即可，列表页画图用）

写操作**只作用于自己**（``user.user.id``）：这组接口就是「个人设置」，不存在「改别人资料」，
所以不用判管理员。按 id 取别人的头像放行给所有登录用户 —— 头像本来就是给人看的（设备 / 用户
列表要画），不算泄露。

两个接口回的是**图片字节**而不是 JSON 壳（``<img src>`` 直接能用）：上传时类型按**文件头**认
（``Content-Type`` 只当提示），下载带 ``ETag`` / ``Last-Modified``，浏览器再请求会拿到 304。
真正的大小 / 类型规则在 :class:`nacho.api.services.profile.ProfileService` 里，这里只翻译。
"""
from __future__ import annotations

import re
from email.utils import formatdate

from fastapi import APIRouter, Request, Response, status

from ...common.dependencies import trace_id_of
from ...common.errors import (
    ApiError,
    AvatarTooLargeError,
    ErrorCode,
    HttpStatus,
)
from ...common.models import ApiResponse, ErrorResponse
from ...services.profile import ProfileService
from ..auth.dependencies import ApiOptionsDep, CurrentUserDep
from .dependencies import ProfileServiceDep
from .requests import UpdateProfileRequest
from .responses import ProfileData

router = APIRouter(prefix="/profile", tags=["个人设置"])

#: user_id 的形状：与头像存储当文件名用的规则一致（见 :mod:`nacho.api.services.profile.store_file`）
_USER_ID_RE: re.Pattern[str] = re.compile(r"^(?!\.)[A-Za-z0-9._-]{1,64}$")


def _ensure_known_user(user_id: str) -> str:
    """先按形状挡一道：不合形状的 id 直接按「没有这个用户」404。

    不挡的话，``../`` 这类值会一路走到存储层 —— 那儿还有一道（抛 ``ValueError``），但那会
    变成 500；在入口判成 404 语义也更对：这种 id 本来就不可能是用户。
    """
    if not _USER_ID_RE.match(user_id):
        raise ApiError(
            ErrorCode.HTTP_ERROR, "没有这个用户", status_code=status.HTTP_404_NOT_FOUND
        )
    return user_id


def _not_found(what: str) -> ApiError:
    """统一的 404（不存在与「不属于你」在别的入口也走同一个码，见 onebot / workflow）。"""
    return ApiError(ErrorCode.HTTP_ERROR, what, status_code=status.HTTP_404_NOT_FOUND)


async def _avatar_response(
    request: Request, service: ProfileService, user_id: str
) -> Response:
    """把头像拼成图片响应：``ETag`` = 「更新时间 + 大小」，客户端带回来就回 304。"""
    found = await service.avatar(user_id)
    if found is None:
        raise _not_found("还没有设置头像")
    info, data = found
    etag: str = f'"{int(info.updated_at)}-{info.size}"'
    headers: dict[str, str] = {
        "ETag": etag,
        # 私人图片：允许浏览器自己留着，但每一轮都要回来问一句（别让共享缓存留下它）
        "Cache-Control": "private, no-cache",
        # 类型以我方嗅出来的为准，不让浏览器再猜（防止「图片」被当成 HTML 执行）
        "X-Content-Type-Options": "nosniff",
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    headers["Last-Modified"] = formatdate(info.updated_at, usegmt=True)
    return Response(content=data, media_type=info.mime, headers=headers)


@router.get(
    "",
    response_model=ApiResponse[ProfileData],
    summary="我的资料与头像",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse, "description": "需要登录"},
    },
)
async def read_profile(
    request: Request,
    user: CurrentUserDep,
    service: ProfileServiceDep,
    options: ApiOptionsDep,
) -> ApiResponse[ProfileData]:
    """取当前登录用户的资料 + 头像信息（个人设置页进来先调它）。"""
    view = await service.get(user.user.id)
    if view is None:
        raise _not_found("没有这个用户")
    return ApiResponse[ProfileData](
        data=ProfileData.from_view(view, prefix=options.prefix),
        trace_id=trace_id_of(request),
    )


@router.patch(
    "",
    response_model=ApiResponse[ProfileData],
    summary="改昵称",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse, "description": "需要登录"},
        HttpStatus.UNPROCESSABLE_ENTITY: {
            "model": ErrorResponse,
            "description": "昵称不合法（空 / 超过 32 个字符）",
        },
    },
)
async def update_profile(
    payload: UpdateProfileRequest,
    request: Request,
    user: CurrentUserDep,
    service: ProfileServiceDep,
    options: ApiOptionsDep,
) -> ApiResponse[ProfileData]:
    """改昵称（只改自己的）；规则与注册时一致：去两端空白后 1-32 个字符。"""
    view = await service.set_nickname(
        user.user.id, payload.nickname, trace_id=trace_id_of(request)
    )
    if view is None:
        raise _not_found("没有这个用户")
    return ApiResponse[ProfileData](
        data=ProfileData.from_view(view, prefix=options.prefix),
        trace_id=trace_id_of(request),
    )


@router.put(
    "/avatar",
    response_model=ApiResponse[ProfileData],
    summary="上传头像",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse, "description": "需要登录"},
        HttpStatus.REQUEST_ENTITY_TOO_LARGE: {
            "model": ErrorResponse,
            "description": "头像超过大小上限",
        },
        HttpStatus.UNSUPPORTED_MEDIA_TYPE: {
            "model": ErrorResponse,
            "description": "不是认得的图片（只收 PNG / JPEG / WebP / GIF）",
        },
        HttpStatus.UNPROCESSABLE_ENTITY: {
            "model": ErrorResponse,
            "description": "内容为空",
        },
    },
)
async def upload_avatar(
    request: Request,
    user: CurrentUserDep,
    service: ProfileServiceDep,
    options: ApiOptionsDep,
) -> ApiResponse[ProfileData]:
    """上传头像：**请求体就是图片字节**（不是 multipart，也不是 JSON）。

    类型只认**文件头**（``Content-Type`` 只当提示，报得再对也不算数）：PNG / JPEG / WebP / GIF。
    超过 ``avatar_max_bytes``（默认 2 MiB）回 413 —— ``Content-Length`` 先粗挡一道，收完再按真实
    长度复查（后者才准）。真要在门口就断掉超大请求，得靠反向代理那一层设 ``client_max_body_size``。
    """
    declared: str | None = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > service.max_avatar_bytes:
        raise AvatarTooLargeError(limit=service.max_avatar_bytes)
    data: bytes = await request.body()
    view = await service.put_avatar(user.user.id, data, trace_id=trace_id_of(request))
    if view is None:
        raise _not_found("没有这个用户")
    return ApiResponse[ProfileData](
        data=ProfileData.from_view(view, prefix=options.prefix),
        trace_id=trace_id_of(request),
    )


@router.get(
    "/avatar",
    summary="我的头像",
    response_class=Response,
    responses={
        status.HTTP_200_OK: {
            "content": {"image/png": {}, "image/jpeg": {}, "image/webp": {}, "image/gif": {}},
            "description": "头像图片字节",
        },
        status.HTTP_304_NOT_MODIFIED: {"description": "没变（带了匹配的 If-None-Match）"},
        status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse, "description": "需要登录"},
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse, "description": "还没设过头像"},
    },
)
async def read_my_avatar(
    request: Request, user: CurrentUserDep, service: ProfileServiceDep
) -> Response:
    """取我的头像（图片字节）；没设过头像回 404，前端据此画默认头像。"""
    return await _avatar_response(request, service, user.user.id)


@router.delete(
    "/avatar",
    response_model=ApiResponse[ProfileData],
    summary="删掉头像",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse, "description": "需要登录"},
    },
)
async def delete_avatar(
    request: Request,
    user: CurrentUserDep,
    service: ProfileServiceDep,
    options: ApiOptionsDep,
) -> ApiResponse[ProfileData]:
    """删掉我的头像；本来就没设过也算成功（结果一样是「没有头像」）。"""
    view = await service.remove_avatar(user.user.id, trace_id=trace_id_of(request))
    if view is None:
        raise _not_found("没有这个用户")
    return ApiResponse[ProfileData](
        data=ProfileData.from_view(view, prefix=options.prefix),
        trace_id=trace_id_of(request),
    )


@router.get(
    "/avatar/{user_id}",
    summary="某人的头像",
    response_class=Response,
    responses={
        status.HTTP_200_OK: {
            "content": {"image/png": {}, "image/jpeg": {}, "image/webp": {}, "image/gif": {}},
            "description": "头像图片字节",
        },
        status.HTTP_304_NOT_MODIFIED: {"description": "没变（带了匹配的 If-None-Match）"},
        status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse, "description": "需要登录"},
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse, "description": "这个人没有头像"},
    },
)
async def read_avatar(
    user_id: str, request: Request, user: CurrentUserDep, service: ProfileServiceDep
) -> Response:
    """取某个用户的头像（登录即可看，展示用）；没有头像回 404。"""
    return await _avatar_response(request, service, _ensure_known_user(user_id))
