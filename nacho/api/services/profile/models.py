"""个人设置的业务层形状：头像元信息，以及「资料 + 头像」的组合视图。"""
from __future__ import annotations

from dataclasses import dataclass

from ..user.models import UserProfile


@dataclass(frozen=True)
class AvatarInfo:
    """一个用户的头像**元信息**（不含字节）：类型 / 大小 / 更新时间。

    ``updated_at`` 是 Unix 秒，两个用处：接口层拿它做 ``ETag``（浏览器带
    ``If-None-Match`` 回来就回 304），前端拿它当换图后的缓存刷新标记
    （``<img src=".../avatar?v=<updated_at>">``）。
    """

    user_id: str
    mime: str
    size: int
    updated_at: float


@dataclass(frozen=True)
class ProfileView:
    """资料 + 头像：个人设置页要的就是这两样；没设过头像时 ``avatar is None``。"""

    profile: UserProfile
    avatar: AvatarInfo | None = None

    @property
    def has_avatar(self) -> bool:
        """有没有头像（前端据此决定画默认头像还是真图）。"""
        return self.avatar is not None
