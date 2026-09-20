"""从「这次登录从哪来」推出设备信息（**纯函数，不认识 HTTP**）。

HTTP 那一步（从请求头和连接里取值）留在入口层（:mod:`nacho.api.api.auth.dependencies`），
这里只做解析，所以能单独测、也不依赖 starlette。

User-Agent 里**没有「设备名称」这个概念**——只有系统、浏览器、是不是移动端。所以设备名
分两步：优先用客户端自报的 ``X-Device-Name``（可能是「我的 iPhone」这种），没有就按
「浏览器 · 系统」拼一个（如 ``Chrome · macOS``），照样能让人认出是哪个设备。
"""
from __future__ import annotations

from .models import DEVICE_DESKTOP, DEVICE_MOBILE, DEVICE_UNKNOWN, ClientInfo

#: 认「是手机」的记号（够用即可，不追求穷尽各家浏览器）
_MOBILE_MARKS: tuple[str, ...] = ("Mobile", "Android", "iPhone", "iPad", "iPod", "Windows Phone")

#: 浏览器识别：**顺序有讲究**——后出现的往往把自己伪装成前面的，所以先认"更具体的"
_BROWSERS: tuple[tuple[str, str], ...] = (
    ("Edg/", "Edge"),
    ("OPR/", "Opera"),
    ("Opera", "Opera"),
    ("MicroMessenger", "微信"),
    ("QQBrowser", "QQ浏览器"),
    ("Firefox/", "Firefox"),
    ("Chrome/", "Chrome"),
    ("Safari/", "Safari"),
    ("curl/", "curl"),
    ("python-requests", "requests"),
)

#: 系统识别：同样是先具体后宽泛（Android 的 UA 里也带 Linux，iOS 的也带 Mac OS X）
_SYSTEMS: tuple[tuple[str, str], ...] = (
    ("Windows NT", "Windows"),
    ("Android", "Android"),
    ("iPhone", "iOS"),
    ("iPad", "iPadOS"),
    ("Mac OS X", "macOS"),
    ("CrOS", "ChromeOS"),
    ("Linux", "Linux"),
)


def _first_match(text: str, table: tuple[tuple[str, str], ...]) -> str:
    """按表里的顺序找第一个命中的记号，返回它对应的名字；都没命中返回空串。"""
    for mark, name in table:
        if mark in text:
            return name
    return ""


def detect_browser(user_agent: str) -> str:
    """从 UA 判浏览器；认不出来返回空串。"""
    return _first_match(user_agent, _BROWSERS)


def detect_os(user_agent: str) -> str:
    """从 UA 判操作系统；认不出来返回空串。"""
    return _first_match(user_agent, _SYSTEMS)


def detect_device_type(user_agent: str) -> str:
    """从 UA 判手机还是电脑：认不出移动记号就是电脑，两边都不认就 ``unknown``。"""
    if not user_agent:
        return DEVICE_UNKNOWN
    if any(mark in user_agent for mark in _MOBILE_MARKS):
        return DEVICE_MOBILE
    if detect_os(user_agent):
        return DEVICE_DESKTOP
    return DEVICE_UNKNOWN


def describe_client(
    *, ip: str = "", user_agent: str = "", device_name: str = ""
) -> ClientInfo:
    """把「ip + 自报设备名 + UA」整理成 :class:`ClientInfo`。

    :param device_name: 客户端自报的设备名（``X-Device-Name``）；空着就按 UA 拼一个。
    """
    browser: str = detect_browser(user_agent)
    system: str = detect_os(user_agent)
    described: str = " · ".join(part for part in (browser, system) if part)
    return ClientInfo(
        ip=ip,
        user_agent=user_agent,
        device_name=device_name.strip() or described or "未知设备",
        device_type=detect_device_type(user_agent),
        browser=browser,
        os=system,
    )
