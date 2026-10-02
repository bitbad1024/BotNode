"""个人设置的响应体：资料 + 头像信息（头像字节本身走独立的图片接口）。"""
from __future__ import annotations

from typing import ClassVar

from pydantic import ConfigDict, Field

from ...common.models import _Frozen
from ...services.profile.models import ProfileView


class ProfileData(_Frozen):
    """个人资料 + 头像信息。

    ``has_avatar=False`` 时，下面那组头像字段都是空值（前端据此画默认头像，别去拉图）。
    ``avatar_url`` 里带 ``?v=<更新时间>``：换了头像以后地址跟着变，等于替前端把缓存刷掉。
    """

    id: str
    account: str
    nickname: str = ""
    roles: list[str] = Field(default_factory=list)
    #: 有没有头像
    has_avatar: bool = False
    #: 头像类型（``image/png`` 这类）与字节数
    avatar_mime: str = ""
    avatar_size: int = 0
    #: 头像最近更新时间（Unix 秒）
    avatar_updated_at: float = 0.0
    #: 头像地址（``<prefix>/profile/avatar/<id>?v=<秒>``；没头像时为空串）
    avatar_url: str = ""

    @classmethod
    def from_view(cls, view: ProfileView, *, prefix: str) -> ProfileData:
        """把业务层的「资料 + 头像」转成对外形状；头像地址由 ``prefix`` 拼出来。"""
        profile = view.profile
        avatar = view.avatar
        return cls(
            id=profile.id,
            account=profile.account,
            nickname=profile.nickname,
            roles=list(profile.roles),
            has_avatar=avatar is not None,
            avatar_mime=avatar.mime if avatar is not None else "",
            avatar_size=avatar.size if avatar is not None else 0,
            avatar_updated_at=avatar.updated_at if avatar is not None else 0.0,
            avatar_url=(
                f"{prefix}/profile/avatar/{profile.id}?v={int(avatar.updated_at)}"
                if avatar is not None
                else ""
            ),
        )

    model_config: ClassVar[ConfigDict] = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "u-admin",
                "account": "admin",
                "nickname": "BotNode",
                "roles": ["admin"],
                "has_avatar": True,
                "avatar_mime": "image/png",
                "avatar_size": 20480,
                "avatar_updated_at": 1758700000.0,
                "avatar_url": "/api/profile/avatar/u-admin?v=1758700000",
            }
        },
        frozen=True,
    )
