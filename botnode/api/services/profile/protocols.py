"""头像存储的能力协议：**只声明不实现**，换底层不用动上层。

上层（:class:`~botnode.api.services.profile.service.ProfileService` 与接口层）只认这一份：
存字节、取字节、问元信息、删掉。落哪、怎么命名、要不要分桶 / 加 CDN，由实现决定 ——
默认实现是 :class:`~botnode.api.services.profile.store_file.FileAvatarStore`（本地目录），
将来要换对象存储（S3 / OSS）或塞进数据库，照着这份协议再写一个即可，上面一行都不用改。

口径与 :mod:`botnode.api.services.user.protocols` 一致：**只收已经校验过的数据**
（类型嗅过、大小量过才进来），存储不负责判断「这算不算头像」。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import AvatarInfo


@runtime_checkable
class AvatarStore(Protocol):
    """头像存储：按 ``user_id`` 存一份图片字节。"""

    async def save(self, user_id: str, data: bytes, *, mime: str) -> AvatarInfo:
        """写入（覆盖）某人的头像，返回落好之后的元信息。

        ``mime`` 是**嗅出来的**真实类型（不是客户端报的那个），实现可以据此决定扩展名 /
        对象名 / 附带属性；``user_id`` 由实现负责当成「名字」而不是「路径」用（别让人从
        这里跳出存储范围）。
        """
        ...

    async def info(self, user_id: str) -> AvatarInfo | None:
        """问元信息；这个人没有头像时返回 ``None``。"""
        ...

    async def load(self, user_id: str) -> tuple[AvatarInfo, bytes] | None:
        """取头像字节与元信息；没有时返回 ``None``。"""
        ...

    async def remove(self, user_id: str) -> bool:
        """删掉某人的头像；真删到了返回 ``True``，本来就没有返回 ``False``。"""
        ...
