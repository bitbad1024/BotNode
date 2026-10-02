"""接口层的日志接入点。

接的是 ``botnode.core.logger`` 那套进程门面，两个名字各管一路::

    api           业务日志（登录成功 / 失败、令牌校验失败……）
    api.access    访问日志（每条请求一行：方法、路径、状态码、耗时、trace_id）

两个名字都是**所有者**字段的填写方：登录用户自己发起的请求记在他名下，没登录的请求
（登录接口本身、被拒的请求）算**公共所有者**——谁的操作一目了然，见
:data:`OWNER_ID_STATE`。

核心由装配层经 :func:`set_core` 存进本模块的槽位（组合根 ``botnode.bootstrap`` 或
:func:`botnode.wiring.wire_loggers` 负责）。``import`` 本模块**零副作用** —— 没装配就调用
:func:`api_logger` 会当场抛错（fail fast），不会默默按默认参数建一份把配置定死的核心。

**不单独挂文件出口**：文件出口是整进程**一份**、按天分片的（见
:class:`~botnode.core.logger.processors.LocalFileLogProcessor`），接口层 / OneBot / 工作流的日志
都进那一份，靠记录里的 ``logger_name``（``botnode.api`` / ``botnode.api.access``）区分来源 ——
想只看接口层那一路就按 ``logger_name`` 检索，不必再拆文件。

访问日志与业务日志的**唯一区别**是落库那份：访问日志一次请求一条，只配给人翻文件，
进了库会把「谁在什么时候干了什么」（登录、开会话、签发 / 吊销令牌……）淹掉 —— 所以
``api.access`` 那条路上的落库通道在这里直接堵死：文件与控制台照收，只有库不收。这份
静音是取的时候**每次带上**的（视图，不发布、不登记），所以没有「装配顺序」这回事。

逐条请求的编号与访问日志在 :mod:`botnode.api.middlewares.request_log` 里 —— 那一块是「每次
请求都要做的事」，跟这里的「名字与通道」分开。
"""
from __future__ import annotations

from typing import cast

from botnode.core.logger import BaseLogger

#: api 层业务日志的名字（**相对核心**：核心名 botnode -> botnode.api）
API_LOGGER_NAME: str = "api"
#: 访问日志的名字（``api`` 的子实例）
ACCESS_LOGGER_NAME: str = "api.access"
#: 请求编号的响应头；请求自带同名头就沿用（链路上的上游已经编过号了）
TRACE_ID_HEADER: str = "X-Trace-Id"
#: 当前登录用户挂在 ``request.state`` 上的属性名：鉴权依赖写、请求日志中间件读，
#: 作为访问日志的 ``owner_id``（谁的操作）。没走鉴权的请求不写 —— 空串 = 公共所有者。
OWNER_ID_STATE: str = "owner_id"
#: 落库出口的通道名（全局留存出口；名字取自 ``DatabaseLogProcessor.name``）
DATABASE_CHANNEL_NAME: str = "database"

#: 装配槽位：组合根建好核心后存进来；``None`` = 尚未装配
_core: BaseLogger | None = None


def set_core(core: BaseLogger | None) -> None:
    """装配日志核心（传 ``None`` 清除槽位，测试收尾用）。"""
    global _core
    _core = core


def api_logger(name: str = API_LOGGER_NAME) -> BaseLogger:
    """取接口层的日志实例；未装配时抛错（见模块文档）。

    ``api.access`` 是访问日志：落库那份是审计时间线，一次请求一条的流水进去了只会把它
    淹掉 —— 所以这条路的落库通道**每次取都堵上**（``child`` 挂命名层级、``bind`` 出
    视图、``mute`` 堵库），文件与控制台照常收。
    """
    if _core is None:
        raise RuntimeError(
            "api_logger 尚未装配：先调 botnode.wiring.wire_loggers(core) 或 "
            "botnode.api.logging.set_core(core)"
        )
    node = cast(BaseLogger, cast(object, _core.child(name)))
    if name == ACCESS_LOGGER_NAME:
        return node.bind().mute(DATABASE_CHANNEL_NAME)
    return node
