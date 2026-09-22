"""接口层的日志接入点。

接的是 ``nacho.core.logger`` 那套进程门面，两个名字各管一路::

    api           业务日志（登录成功 / 失败、令牌校验失败……）
    api.access    访问日志（每条请求一行：方法、路径、状态码、耗时、trace_id）

两个名字都是**所有者**字段的填写方：登录用户自己发起的请求记在他名下，没登录的请求
（登录接口本身、被拒的请求）算**公共所有者**——谁的操作一目了然，见
:data:`OWNER_ID_STATE`。

两个名字是**父子关系**：``api.access`` 派生自 ``api``，所以它没挂自己的出口时会落到
``api`` 那一份配置上（日志系统的「自层覆盖 / 回落」规则），一处挂载两路日志都进同一个
文件。要给访问日志单独落文件，就再 ``attach_api_logging(..., name=ACCESS_LOGGER_NAME)``
挂一次。

接入动作就一个函数::

    from nacho.api import attach_api_logging

    attach_api_logging(Path("logs/api.log"))   # 先挂载、再取实例（落回配置创建即冻结）

它把 :class:`~nacho.core.logger.processors.LocalFileLogProcessor` 挂到 ``api`` 这个名字上，
于是 api 层的日志单独进 ``logs/api.log``，**不混进核心的** ``nacho.log``。

逐条请求的编号与访问日志在 :mod:`nacho.api.middlewares.request_log` 里 —— 那一块是「每次
请求都要做的事」，跟这里的「挂载什么出口」分开。
"""
from __future__ import annotations

from pathlib import Path

from nacho.core.logger import BaseLogger, LocalFileLogProcessor, attach_mount, get_logger

#: api 层业务日志的名字（**相对核心**：核心名 nacho -> nacho.api）
API_LOGGER_NAME: str = "api"
#: 访问日志的名字（``api`` 的子实例）
ACCESS_LOGGER_NAME: str = "api.access"
#: 请求编号的响应头；请求自带同名头就沿用（链路上的上游已经编过号了）
TRACE_ID_HEADER: str = "X-Trace-Id"
#: 当前登录用户挂在 ``request.state`` 上的属性名：鉴权依赖写、请求日志中间件读，
#: 作为访问日志的 ``owner_id``（谁的操作）。没走鉴权的请求不写 —— 空串 = 公共所有者。
OWNER_ID_STATE: str = "owner_id"


def api_logger(name: str = API_LOGGER_NAME) -> BaseLogger:
    """取接口层的日志实例（没 ``configure`` 过会顺带建一个默认核心）。"""
    return get_logger(name)


def attach_api_logging(
    path: Path | str,
    *,
    name: str = API_LOGGER_NAME,
    buffer_size: int = 200,
    flush_interval: float = 2.0,
    max_bytes: int | None = None,
    backup_count: int = 3,
) -> BaseLogger:
    """把接口层的日志单独落一个文件：挂到 ``name`` 上（自层覆盖，不进核心的日志）。

    **先挂载、再取实例**：日志系统的落回配置是派生那一刻复制、创建即冻结的，挂载晚了
    已经取出的实例拿不到这个出口。

    :param path: 日志文件路径；``max_bytes`` 给值时按大小切割，保留 ``backup_count`` 份历史。
    :return: 挂好出口的那个日志实例（``get_logger(name)``）。
    """
    processor = LocalFileLogProcessor(
        Path(path),
        name=f"{name}.file",
        buffer_size=buffer_size,
        flush_interval=flush_interval,
        max_bytes=max_bytes,
        backup_count=backup_count,
    )
    attach_mount(name, processor)
    return get_logger(name)
