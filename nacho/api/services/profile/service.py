"""个人设置的业务编排：改昵称 + 头像的存 / 取 / 删。

个人设置页要的是「资料 + 头像」两样，而这两样落在两个地方：昵称在用户表里
（:class:`~nacho.api.services.user.protocols.UserStore`），头像字节在头像存储里
（:class:`~nacho.api.services.profile.protocols.AvatarStore`）。本类把它们串起来，并把
「空不空 / 多大 / 是不是认得的图」这几条规则拿住 —— 规则只在这里写一遍，换个入口（CLI、
消息入口）照样管用。

与别的业务层一样：不认识 FastAPI，失败抛 :class:`~nacho.api.common.errors.ApiError`
（状态码已经挂在异常上）。HTTP 入口在 :mod:`nacho.api.api.profile`。
"""
from __future__ import annotations

from nacho.core.logger import BaseLogger

from ...common.errors import AvatarTooLargeError, AvatarTypeUnsupportedError, ValidationError
from ...logging import API_LOGGER_NAME, api_logger
from ...options import DEFAULT_AVATAR_MAX_BYTES
from ..user.models import UserRecord, profile_of
from ..user.protocols import UserStore
from .images import sniff_image_type
from .models import AvatarInfo, ProfileView
from .protocols import AvatarStore


class ProfileService:
    """个人设置：资料（昵称）+ 头像。"""

    def __init__(
        self,
        users: UserStore,
        avatars: AvatarStore,
        *,
        max_avatar_bytes: int = DEFAULT_AVATAR_MAX_BYTES,
        logger: BaseLogger | None = None,
    ) -> None:
        """
        :param users: 用户存储（昵称落在这张表上）；
        :param avatars: 头像存储（换本地目录 / 对象存储只换这一份实现）；
        :param max_avatar_bytes: 头像字节上限，超了抛 413；
        :param logger: 业务日志实例，默认 ``api`` 那个。
        """
        self._users: UserStore = users
        self._avatars: AvatarStore = avatars
        self._max_bytes: int = max_avatar_bytes
        self._logger: BaseLogger | None = logger

    @property
    def avatars(self) -> AvatarStore:
        """头像存储（测试与扩展时会用到，见 :mod:`.protocols`）。"""
        return self._avatars

    @property
    def max_avatar_bytes(self) -> int:
        """头像字节上限。"""
        return self._max_bytes

    # ------------------------------------------------------------------ 资料
    async def get(self, user_id: str) -> ProfileView | None:
        """取资料 + 头像元信息；用户不存在返回 ``None``（调用方报 404）。"""
        user: UserRecord | None = await self._users.get_by_id(user_id)
        if user is None:
            return None
        return ProfileView(profile=profile_of(user), avatar=await self._avatars.info(user_id))

    async def set_nickname(
        self, user_id: str, nickname: str, *, trace_id: str = "-"
    ) -> ProfileView | None:
        """改昵称；用户不存在返回 ``None``。

        昵称规则不在这里判：它由请求层的 :data:`~nacho.api.services.user.validation.Nickname`
        保证（去空白 + 长度），服务层只负责落库与记账。
        """
        user: UserRecord | None = await self._users.set_nickname(user_id, nickname)
        if user is None:
            return None
        self._log().info("昵称已更新", owner_id=user_id, nickname=nickname, trace_id=trace_id)
        return ProfileView(profile=profile_of(user), avatar=await self._avatars.info(user_id))

    # ------------------------------------------------------------------ 头像
    async def put_avatar(
        self, user_id: str, data: bytes, *, trace_id: str = "-"
    ) -> ProfileView | None:
        """存头像；用户不存在返回 ``None``。

        三道门按「便宜的先来」排：**空**（零长）-> **大小**（量一下字节数）-> **类型**
        （嗅探只看前 16 字节）。前两道是 422 / 413，类型那道是 415 —— 都只有一条统一口径，
        免得入口层各判各的。
        """
        user: UserRecord | None = await self._users.get_by_id(user_id)
        if user is None:
            return None
        if not data:
            raise ValidationError("头像内容是空的")
        if len(data) > self._max_bytes:
            raise AvatarTooLargeError(limit=self._max_bytes)
        mime: str | None = sniff_image_type(data)
        if mime is None:
            raise AvatarTypeUnsupportedError()
        info: AvatarInfo = await self._avatars.save(user_id, data, mime=mime)
        self._log().info(
            "头像已更新",
            owner_id=user_id,
            mime=info.mime,
            size=info.size,
            trace_id=trace_id,
        )
        return ProfileView(profile=profile_of(user), avatar=info)

    async def avatar(self, user_id: str) -> tuple[AvatarInfo, bytes] | None:
        """取头像字节 + 元信息；没有返回 ``None``（调用方回 404，前端画默认头像）。"""
        return await self._avatars.load(user_id)

    async def remove_avatar(self, user_id: str, *, trace_id: str = "-") -> ProfileView | None:
        """删头像；用户不存在返回 ``None``。

        本来就没设过也算成功（结果同样是「没有头像」），只是不记那条「已删除」日志 ——
        这不是错误，前端连点两下删除不该看到失败。
        """
        user: UserRecord | None = await self._users.get_by_id(user_id)
        if user is None:
            return None
        if await self._avatars.remove(user_id):
            self._log().info("头像已删除", owner_id=user_id, trace_id=trace_id)
        return ProfileView(profile=profile_of(user), avatar=None)

    def _log(self) -> BaseLogger:
        """业务日志实例（``api``）；用完即取，避免核心被替换后拿到旧的。"""
        return self._logger if self._logger is not None else api_logger(API_LOGGER_NAME)
