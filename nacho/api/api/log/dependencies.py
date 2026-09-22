"""日志路由的注入件（``Depends`` 的那些东西）。

单独一份是为了让 ``router.py`` 只剩「接口长什么样」：日志实例从哪来、谁能在它上面查，
堆在路由里会盖住接口本身。

**登录校验直接复用鉴权那份**（:data:`nacho.api.api.auth.dependencies.CurrentUserDep`）：
查日志用的就是登录令牌，没有第二套凭据。**范围**复用 OneBot 那组接口的规则
（:func:`~nacho.api.api.onebot.dependencies.is_admin` /
:func:`~nacho.api.api.onebot.dependencies.ensure_can_touch`）—— 管理员不限，其余人只限
自己的 ``id``。
"""

from __future__ import annotations

from typing import Annotated, Protocol, cast

from fastapi import Depends, Request

from nacho.core.logger import BaseLogger


class _AppState(Protocol):
    """挂在 ``app.state`` 上的东西（由 :func:`nacho.api.create_app` 写入）。"""

    logger: BaseLogger


class _App(Protocol):
    """FastAPI 的 ``app``，这里只关心它身上的 ``state``。"""

    state: _AppState


def get_app_logger(request: Request) -> BaseLogger:
    """取日志实例：装配时挂在 ``app.state.logger`` 上（见 :func:`nacho.api.create_app`）。

    它与日志核心**共享同一个出口注册表**，所以 ``search`` 看得见核心下挂的所有出口
    （数据库 / 文件 / 控制台），查询条件一路透到各自的 ``search``。

    ``app.state`` 是运行时挂上去的，类型检查看不到，所以把 ``request.app`` 先 ``cast`` 成
    带 ``state.logger`` 的形状，避免整条链都是 ``Any``。
    """
    app = cast("_App", request.app)
    return app.state.logger


#: 依赖简写：路由函数里写 ``logger: LoggerDep`` 即可
LoggerDep = Annotated[BaseLogger, Depends(get_app_logger)]


__all__ = [
    "LoggerDep",
    "get_app_logger",
]
