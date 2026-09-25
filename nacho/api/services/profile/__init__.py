"""个人设置域：改昵称 + 头像（存 / 取 / 删）。

    models.py      AvatarInfo / ProfileView（业务层形状）
    images.py      图片嗅探：按文件头认类型（不信 Content-Type），只收 png/jpeg/webp/gif
    protocols.py   AvatarStore：头像存储要什么能力（换底层只换实现）
    store_file.py  默认实现：一个目录一个用户一个文件（目录来自 ``[api] avatar_dir``）
    service.py     编排：资料 + 头像两条路串起来，规则（空 / 大小 / 类型）只判一遍

依赖方向是**单向**的：本模块用 :mod:`nacho.api.services.user`（昵称在用户表里），
``user`` 不认识本模块。业务层不 import :mod:`nacho.api.api`（入口层）—— HTTP 入口在
:mod:`nacho.api.api.profile`，两边由 :func:`nacho.api.create_app` 装配。
"""
from __future__ import annotations

from .images import ALLOWED_IMAGE_TYPES, sniff_image_type
from .models import AvatarInfo, ProfileView
from .protocols import AvatarStore
from .service import DEFAULT_AVATAR_MAX_BYTES, ProfileService
from .store_file import FileAvatarStore

__all__ = [
    "ALLOWED_IMAGE_TYPES",
    "DEFAULT_AVATAR_MAX_BYTES",
    "AvatarInfo",
    "AvatarStore",
    "FileAvatarStore",
    "ProfileService",
    "ProfileView",
    "sniff_image_type",
]
