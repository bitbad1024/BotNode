"""图片嗅探：只看**字节开头**判断是什么图，不信客户端报的 ``Content-Type``。

为什么自己认：``Content-Type`` 是客户端说了算的 —— 报成 ``image/png`` 的 HTML 一样能传上来，
存下来再当图片发给别人的浏览器看，等于送人一个 XSS 入口。所以类型从**文件头**认，认不出来就不收。

只收四种浏览器画得出来的位图（png / jpeg / webp / gif），**SVG 不在其列**：那是 XML，能内嵌
脚本，当图片直接回给浏览器就是脚本执行。真要做到像素级还得解码，那是引入 Pillow 之后的事 ——
现在不做服务端图片处理（不缩放、不转码、不抠透明通道）。
"""
from __future__ import annotations

from typing import Final

#: 允许的头像类型：mime -> 落盘扩展名（扩展名只用来命名文件，回给客户端的永远是 mime）
ALLOWED_IMAGE_TYPES: Final[dict[str, str]] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

#: 从扩展名反查 mime（读文件时用：落盘只留了扩展名）
MIME_BY_SUFFIX: Final[dict[str, str]] = {
    suffix: mime for mime, suffix in ALLOWED_IMAGE_TYPES.items()
}

#: 认文件头要看多少字节（WEBP 要看到第 12 个字节）
SNIFF_BYTES: Final[int] = 16


def sniff_image_type(data: bytes) -> str | None:
    """按文件头认图片类型，返回 mime；不认识的返回 ``None``。"""
    head: bytes = data[:SNIFF_BYTES]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(head) >= 12 and head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def extension_for(mime: str) -> str:
    """mime 对应的落盘扩展名；没登记的抛 :class:`KeyError`（调用方先嗅过类型）。"""
    return ALLOWED_IMAGE_TYPES[mime]
