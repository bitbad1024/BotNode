"""头像存储的默认实现：一个目录，一个用户一个文件。

文件名 = ``<user_id><扩展名>``（扩展名按嗅出来的类型定，见 :mod:`.images`）；同一个人换图就是
换扩展名，所以查找按**前缀**做 —— 一个用户最多留一个文件，找到的就是它。

几条实现上的取舍：

* **user_id 得当文件名用**：只放行 ``[A-Za-z0-9._-]``，别的一律拒绝 —— 否则 ``../`` 这类值
  能把文件写到目录外面去（账号字符集本来就窄，正常值一定通过；接口层还会先挡一道，见
  :mod:`nacho.api.api.profile.router`）；
* **改写走临时文件 + ``os.replace``**：原子替换，读的人不会看到写了一半的图；
* **目录在第一次写入时才建**（``mkdir(parents=True, exist_ok=True)``）：头像目录配错了，
  就到真上传时才报，不该连带让别的接口起不来；
* **文件 IO 丢到线程里**（:func:`asyncio.to_thread`）：事件循环还要伺候别的请求，
  几 MB 的读写不该卡住它。
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

from .images import MIME_BY_SUFFIX, extension_for
from .models import AvatarInfo

#: user_id 能当文件名用的形状；**不许以点开头** —— 点开头是写一半的临时文件记号，
#: 真按它命名会被查找逻辑当残留跳过（``.`` / ``..`` 这类也顺带挡住）
_SAFE_ID: re.Pattern[str] = re.compile(r"^(?!\.)[A-Za-z0-9._-]{1,64}$")


def _check_id(user_id: str) -> str:
    """挡住能跳出目录的用户 id（空、带斜杠、``..``、绝对路径）。

    正常调用不会走到这里（接口层已经挡过）：真走到说明是自己人写错了，直接抛，
    别把「写到了意想不到的地方」这种事故藏成一个静默的 404。
    """
    if not _SAFE_ID.match(user_id):
        raise ValueError(f"用户 id 不能当文件名用：{user_id!r}")
    return user_id


def _info_of(user_id: str, path: Path) -> AvatarInfo:
    """按文件本身算元信息：大小取文件大小、时间取 mtime。

    ``save`` 与 ``info`` 于是看到同一份真相（都以文件为准），不必额外记一份元数据 ——
    将来换成对象存储时，这份「元信息从哪来」由那个实现自己决定。
    """
    stat = path.stat()
    return AvatarInfo(
        user_id=user_id,
        mime=MIME_BY_SUFFIX.get(path.suffix.lower(), "application/octet-stream"),
        size=stat.st_size,
        updated_at=stat.st_mtime,
    )


class FileAvatarStore:
    """把头像按 ``<目录>/<user_id><扩展名>`` 落在本地磁盘上。"""

    def __init__(self, directory: Path | str) -> None:
        """``directory`` 就是配置里的 ``[api] avatar_dir``（默认 ``data/avatars``）。"""
        self._dir: Path = Path(directory)

    @property
    def directory(self) -> Path:
        """头像存放目录（也是「换存储」时唯一要交代的东西）。"""
        return self._dir

    async def save(self, user_id: str, data: bytes, *, mime: str) -> AvatarInfo:
        """写入（覆盖）：临时文件 + ``os.replace`` 原子替换，换扩展名时清掉旧文件。"""
        _check_id(user_id)
        suffix: str = extension_for(mime)
        path: Path = await asyncio.to_thread(self._write, user_id, suffix, data)
        return _info_of(user_id, path)

    async def info(self, user_id: str) -> AvatarInfo | None:
        """问元信息；没有头像返回 ``None``。"""
        _check_id(user_id)
        path: Path | None = await asyncio.to_thread(self._find, user_id)
        return None if path is None else _info_of(user_id, path)

    async def load(self, user_id: str) -> tuple[AvatarInfo, bytes] | None:
        """取字节 + 元信息；没有返回 ``None``。"""
        _check_id(user_id)
        path: Path | None = await asyncio.to_thread(self._find, user_id)
        if path is None:
            return None
        data: bytes = await asyncio.to_thread(path.read_bytes)
        return _info_of(user_id, path), data

    async def remove(self, user_id: str) -> bool:
        """删掉某人的头像（换过扩展名的残留一并删）；删到了返回 ``True``。"""
        _check_id(user_id)
        return await asyncio.to_thread(self._remove, user_id)

    # ------------------------------------------------------------------ 线程里跑的文件活
    def _write(self, user_id: str, suffix: str, data: bytes) -> Path:
        """落盘并返回最终路径（在 :func:`asyncio.to_thread` 里跑）。"""
        self._dir.mkdir(parents=True, exist_ok=True)
        target: Path = self._dir / f"{user_id}{suffix}"
        temp: Path = self._dir / f".{user_id}{suffix}.tmp"
        temp.write_bytes(data)
        os.replace(temp, target)  # 原子替换：读的人不会看到写了一半的图
        for other in self._dir.glob(f"{user_id}.*"):
            if other != target and not other.name.startswith("."):
                other.unlink(missing_ok=True)  # png 换成 jpg：旧的别留着
        return target

    def _find(self, user_id: str) -> Path | None:
        """按前缀找这个人的头像文件；没有（或目录还不存在）返回 ``None``。"""
        if not self._dir.is_dir():
            return None
        for path in sorted(self._dir.glob(f"{user_id}.*")):
            if path.name.startswith("."):
                continue  # 写到一半的临时文件
            if path.suffix.lower() in MIME_BY_SUFFIX:
                return path
        return None

    def _remove(self, user_id: str) -> bool:
        """删掉这个人的全部头像文件；删到了返回 ``True``。"""
        if not self._dir.is_dir():
            return False
        found: bool = False
        for path in self._dir.glob(f"{user_id}.*"):
            if path.name.startswith("."):
                continue
            path.unlink(missing_ok=True)
            found = True
        return found
