"""业务装配：入口把「日志核心 + 库引擎」备好之后，由本模块把 nacho 自己这几块挂起来跑。

与根目录 ``app.py`` 的分工（对看）::

    app.py           读配置 -> 按配置建日志核心 -> 建库引擎 -> 交给本模块 -> 停机收尾
    nacho/bootstrap  建表与存储 -> 起接口层 HTTP -> 起 OneBot 反向 WS -> 起调度器
                     -> 载入已发布工作流 -> 停机（含冲刷日志余量、关库连接）

为什么要在意这个顺序：日志核心必须在**任何业务模块被 import 之前**按配置建好。nacho 里有模块
级 ``get_logger``（导入即执行）——谁先被 import，谁就顺手把进程默认核心按默认参数建出来，配置
里的颜色 / 级别就此定死、再也传不进去（``LogManager.configure`` 在「已存在核心」时只合并
processors）。入口因此不在顶层 import 业务模块，本模块也是**建好核心之后**才被导入。

依赖方向：本模块认识 nacho 的各业务包；反过来不成立 —— 入口只认识本模块，不认识业务。
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path

import uvicorn
from sqlalchemy.ext.asyncio import AsyncEngine

from .api import (
    ApiOptions,
    SqlSessionStore,
    SqlUserStore,
    attach_api_logging,
    create_app,
)
from .core.cache import CacheOptions, cache
from .core.logger import BaseLogger, get_logger, manager
from .core.scheduler import scheduler
from .onebot import (
    ONEBOT_LOGGER_NAME,
    OneBotConnection,
    OneBotEvent,
    OneBotOptions,
    OneBotServer,
    SqlTokenRegistry,
    attach_onebot_logging,
    onebot_logger,
)
from .workflow import SqlWorkflowStore
from .workflow.runtime import load_published_workflows

# --------------------------------------------------------------------------- 状态
#: 接口层 HTTP 服务（随主程序由 uvicorn 起）；停机时取用
_api_server: uvicorn.Server | None = None
_api_task: asyncio.Task[None] | None = None
#: 入口建好、交给本模块共用的数据库引擎；停机时 dispose
_db_engine: AsyncEngine | None = None
#: OneBot 反向 WS 服务；停机时一并停
_onebot_server: OneBotServer | None = None


class _NoSignalServer(uvicorn.Server):
    """随主程序跑时信号由主程序统一管：覆盖掉 uvicorn 自带的 SIGINT 安装，免得抢了业务循环的停机。"""

    def install_signal_handlers(self) -> None:
        pass


# --------------------------------------------------------------------------- 落库
async def _prepare_stores(
    db: AsyncEngine, log: BaseLogger
) -> tuple[SqlTokenRegistry, SqlUserStore, SqlSessionStore, SqlWorkflowStore]:
    """建表 + 种演示账号（幂等）：各份落库存储都挂同一个 ``db``。

    放在**启动阶段**而不是等 lifespan：接口服务是 ``create_task`` 起的，启动阶段抛的异常没人
    await、会被静默吞掉，于是「没建成」只在第一个请求时才炸成 1146（表不存在），离真正的原因
    很远。放这里：失败就是启动失败，当场看得见。（lifespan 里那次留着兜底，幂等。）
    """
    tokens = SqlTokenRegistry(db)
    await tokens.ensure_schema()
    users = SqlUserStore(db)  # hasher 默认 PBKDF2，只有 seed_demo 用
    await users.ensure_schema()
    seeded = await users.seed_demo()  # 空表才种演示账号，已有数据不动
    sessions = SqlSessionStore(db)
    await sessions.ensure_schema()
    workflows = SqlWorkflowStore(db)
    await workflows.ensure_schema()
    log.info(
        "数据表就绪",
        tables=[
            "onebot_tokens",
            "users",
            "auth_sessions",
            "workflow_definitions",
            "workflow_versions",
        ],
        demo_accounts=seeded,  # 0 = 表里本来就有账号，一条没动
    )
    return tokens, users, sessions, workflows


# --------------------------------------------------------------------------- 业务
async def on_event(conn: OneBotConnection, event: OneBotEvent) -> None:
    """OneBot 事件钩子：业务接这里。

    现在只记一条日志，演示「收到了事件」；要发动作就这么写::

        await conn.call("send_msg", message_type="private", user_id=..., message="hi")
    """
    log = onebot_logger(ONEBOT_LOGGER_NAME)
    log.info("收到事件", post_type=event.post_type, self_id=event.self_id, remote=conn.remote)


# --------------------------------------------------------------------------- 装配
async def run(
    *,
    engine: AsyncEngine,
    logs_dir: Path,
    api: Mapping[str, object],
    api_host: str,
    api_port: int,
    onebot: Mapping[str, object],
    cache_config: Mapping[str, object],
) -> None:
    """把业务挂起来（不阻塞）：建表 -> 起接口层 -> 起 OneBot -> 起调度器 -> 载入已发布工作流。

    :param engine: 入口建好的共用引擎（与日志库出口默认是同一个）；
    :param logs_dir: 日志文件目录（接口层 / OneBot 各落一份）；
    :param api: ``[api]`` 那块配置，交给 ``ApiOptions.from_mapping``；
    :param api_host / api_port: 接口层监听地址 —— 这两个归入口管（``ApiOptions`` 里没有，它
        只管前缀与令牌有效期）；
    :param onebot: ``[onebot]`` 那块配置，交给 ``OneBotOptions.from_mapping``；
    :param cache_config: ``[cache]`` 那块配置，交给 ``CacheOptions.from_mapping``。
    """
    global _api_server, _api_task, _db_engine, _onebot_server
    _db_engine = engine
    log = get_logger("bootstrap")

    # 缓存：默认（memory）就是本地内存，配了 redis 而连不上时按 fallback_to_memory 处理
    cache.configure(CacheOptions.from_mapping(cache_config))
    await cache.start()

    tokens, users, sessions, workflows = await _prepare_stores(engine, log)

    # OneBot 反向 WS：先建好对象，下面的接口层要用它（<prefix>/onebot/* 那组管理接口）
    attach_onebot_logging(logs_dir / "onebot.log")
    _onebot_server = OneBotServer(
        OneBotOptions.from_mapping(onebot),
        handler=on_event,
        tokens=tokens,  # 令牌 -> 账号；一个端口接多个客户端，靠它认归属
    )

    # 接口层：挂日志 -> 建应用（注入同一个 db 上的三份存储 + OneBot）-> 起 uvicorn
    attach_api_logging(logs_dir / "api.log")
    options = ApiOptions.from_mapping(api)
    _api_server = _NoSignalServer(
        uvicorn.Config(
            create_app(
                options,
                user_store=users,
                session_store=sessions,
                onebot=_onebot_server,
                workflow_store=workflows,
            ),
            host=api_host,
            port=api_port,
            log_config=None,  # 不接管日志系统（接口层走 nacho.core.logger）
            access_log=False,  # 访问日志交给接口层自己的中间件
        )
    )
    _api_task = asyncio.create_task(_api_server.serve(), name="api")

    # 启动定时任务调度器：开始节点（trigger=time）靠它到点触发
    await scheduler.start()

    # 把所有已发布工作流载入调度器（时间触发的开始节点在此登记 cron）。
    # 发布接口只挪指针、不执行图，触发配置随启动统一生效。
    await load_published_workflows(workflows, scheduler)


async def serve_forever() -> None:
    """主协程停在这：OneBot 起监听并一直跑；端口被占等当场抛 ``OSError``（怎么退由入口定）。"""
    if _onebot_server is None:
        raise RuntimeError("还没装配：先跑 run()")
    await _onebot_server.serve_forever()


# --------------------------------------------------------------------------- 收尾
async def shutdown() -> None:
    """收尾：先停对外的两个服务（接口层 HTTP + OneBot 反向 WS）-> 等调度器跑完在飞的任务
    -> 冲刷日志余量 -> 关库连接。

    顺序不能反：任务里还会写日志，得等它们收尾了再冲刷、关库，收尾日志才不会丢 —— 所以
    ``manager.stop()``（冲刷余量，可能写库）必须在关引擎之前。
    """
    if _api_server is not None:
        _api_server.should_exit = True  # 让 uvicorn 优雅退出（在飞的请求处理完再关）
    if _api_task is not None:
        with suppress(asyncio.CancelledError):
            await _api_task
    if _onebot_server is not None:
        await _onebot_server.stop()  # 关监听并断开所有客户端
    await scheduler.stop()  # 等在飞的任务自然收尾（默认 5 秒，超时只记 warning，不强杀）
    await cache.stop()  # 再停缓存：任务收完了，后面不会再有业务来读写
    await manager.stop()  # 停机自动冲刷余量
    if _db_engine is not None:
        await _db_engine.dispose()
