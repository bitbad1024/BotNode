"""接口层的日志接入点。

接的是 ``nacho.core.logger`` 那套进程门面，两个名字各管一路::

    api           业务日志（登录成功 / 失败、令牌校验失败……）
    api.access    访问日志（每条请求一行：方法、路径、状态码、耗时、trace_id）

两个名字都是**所有者**字段的填写方：登录用户自己发起的请求记在他名下，没登录的请求
（登录接口本身、被拒的请求）算**公共所有者**——谁的操作一目了然，见
:data:`OWNER_ID_STATE`。

**不单独挂文件出口**：文件出口是整进程**一份**、按天分片的（见
:class:`~nacho.core.logger.processors.LocalFileLogProcessor`），接口层 / OneBot / 工作流的日志
都进那一份，靠记录里的 ``logger_name``（``nacho.api`` / ``nacho.api.access``）区分来源 ——
想只看接口层那一路就按 ``logger_name`` 检索，不必再拆文件。

访问日志与业务日志的**唯一区别**是落库那份：访问日志一次请求一条，只配给人翻文件，
进了库会把「谁在什么时候干了什么」（登录、开会话、签发 / 吊销令牌……）淹掉 —— 所以用
:func:`keep_access_off_audit` 把它的落库通道静音，文件与控制台照收。

逐条请求的编号与访问日志在 :mod:`nacho.api.middlewares.request_log` 里 —— 那一块是「每次
请求都要做的事」，跟这里的「名字与通道」分开。
"""
from __future__ import annotations

from nacho.core.logger import BaseLogger, get_logger

#: api 层业务日志的名字（**相对核心**：核心名 nacho -> nacho.api）
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


def api_logger(name: str = API_LOGGER_NAME) -> BaseLogger:
    """取接口层的日志实例（没 ``configure`` 过会顺带建一个默认核心）。"""
    return get_logger(name)


def keep_access_off_audit(channel: str = DATABASE_CHANNEL_NAME) -> BaseLogger:
    """访问日志只进文件与控制台、**不进审计库**：把 ``api.access`` 实例的落库通道静音。

    落库出口是**全局留存出口**，会跟着落回配置进到每一路日志；而访问日志一次请求一条、
    只配给人翻文件，进了库只会把「谁在什么时候干了什么」的审计事件（登录、开会话、
    签发 / 吊销令牌……）淹掉。落库的那份要当审计时间线用（网页 ``/logs`` 查的就是它），
    所以在装配时给 ``api.access`` 这份实例配置把库通道 mute 掉：文件与控制台照常收，
    只有库不收。

    :param channel: 要静音的通道名，默认落库出口那个（``"database"``）。
    :return: 静音后的访问日志实例（``api.access``）。
    """
    logger = api_logger(ACCESS_LOGGER_NAME)
    logger.mute(channel)  # mute 是就地改过滤器（返回 None），logger 本身照旧返回
    return logger
