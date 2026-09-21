"""接口层示例：登录怎么跑通、数据协议长什么样、日志怎么接进来。

运行::

    python examples/api_demo.py

需要 ``fastapi`` + ``httpx``（``pip install "nacho[api]" "nacho[dev]"``）。请求不走真实
网络：用 ``httpx.AsyncClient`` 挂 ASGI 传输层直接打进应用，所以示例能一口气跑完。
真要对外服务，用最后第 7 段给的 uvicorn 命令。

按顺序演示这几件事：

0. **日志接入点**：``configure`` 建核心 -> ``attach_api_logging(logs/api.log)`` 把接口层
   日志单独落一个文件（不混进核心的 ``nacho.log``）；业务日志用 ``api``，访问日志用
   它的子名字 ``api.access``；
1. **装配**：``create_app(ApiOptions.from_mapping(settings.api.model_dump()))`` —— 接口层
   不读配置文件，选项由配置系统的 ``[api]`` 一节转成普通映射喂进来；不传用户存储就用
   内存演示账号（admin / robot / guest）；
2. **登录成功**：看响应协议 ``{success, data:{token, expires_in, user}, trace_id}``，
   密码不在响应里；
3. **登录失败**：密码错与账号不存在是同一个 401 码（不泄露账号存不存在），停用账号是
   403、校验不过（账号字符集 / 密码长度）是 422 且 ``details`` 指出哪个字段；
4. **令牌怎么用**：``GET <prefix>/auth/me`` 带 ``Authorization: Bearer <token>``；
   过期 / 被改过的令牌分别回 ``TOKEN_EXPIRED`` / ``TOKEN_INVALID``；
5. **trace_id 串起来**：请求头带 ``X-Trace-Id`` 就沿用，响应头、响应体、日志里是同一个号；
6. **看日志落了什么**：flush 之后读 ``logs/api.log``（每行一个 JSON）；
7. **起真服务**：uvicorn 命令（这里不真的起，起了就阻塞住）。

演示账号（见 ``nacho.api.services.user.demo.DEMO_USERS``）：
``admin / nacho-admin``（管理员）、``robot / nacho-robot``、``guest / nacho-guest``（已停用）。
"""
from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from config import Settings  # noqa: E402
from nacho.api import (  # noqa: E402
    ApiOptions,
    ApiResponse,
    LoginData,
    attach_api_logging,
    create_app,
)
from nacho.core.logger import configure, manager  # noqa: E402

#: 接口层日志单独落在这里
LOG_PATH: Path = Path(__file__).resolve().parent / "logs" / "api.log"
#: 演示账号
ADMIN: dict[str, str] = {"account": "admin", "password": "nacho-admin"}


def demo_options() -> ApiOptions:
    """读配置里的 ``[api]``，只把访问令牌的有效期钉短一点。

    令牌现在**不是签名**的（是随机串 + 服务端有状态），所以没有"密钥"这回事了；
    之所以还要改一项，只是让示例里的有效期数字好认。
    """
    options: ApiOptions = ApiOptions.from_mapping(Settings.load().api.model_dump())
    return replace(options, token_ttl=1800.0)


def show(title: str, response: httpx.Response) -> None:
    """打印一次请求的结论：状态码 + 响应体（就是对外的数据协议）。"""
    body: Any = response.json()
    brief: str = f"{response.status_code}"
    if isinstance(body, dict) and "error" in body:
        brief += f" {body['error']['code']}：{body['error']['message']}"
    print(f"  {title:<22} -> {brief}")
    print(f"      {body}")


@asynccontextmanager
async def demo_client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    """直连 ASGI 的客户端，并跑一遍 app 的 lifespan。

    ``create_app`` 不接库时会话 / 用户都挂在一块内存 sqlite 上，建表在 lifespan ——
    而 httpx 的 ASGITransport 不会自己触发启动，这里替它跑一遍。
    """
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://demo"
        ) as client:
            yield client


async def main() -> None:
    settings: Settings = Settings.load()
    options: ApiOptions = demo_options()
    prefix: str = options.prefix

    # 0) 日志接入点：先建核心、再挂接口层出口（挂载要在取实例之前）
    core = configure(settings.app.name, level="DEBUG", console=True)
    await core.start()
    attach_api_logging(LOG_PATH)
    print(f"[0] 日志接入：接口层日志落 {LOG_PATH}")

    # 1) 装配：不传 user_store 就兜底挂一块内存 sqlite 并种演示账号；令牌有效期来自 [api].token_ttl
    app = create_app(options)
    print(f"[1] 应用就绪：{prefix}/auth/login，令牌有效期 {options.token_ttl:.0f} 秒")

    async with demo_client(app) as client:
        # 2) 登录成功
        print("[2] 登录成功：")
        ok = await client.post(f"{prefix}/auth/login", json=ADMIN)
        show("POST /auth/login", ok)
        token: str = ApiResponse[LoginData].model_validate(ok.json()).data.token

        # 3) 各种失败：401 / 403 / 422 都是同一个出口形状
        print("[3] 失败与校验：")
        show(
            "密码错",
            await client.post(
                f"{prefix}/auth/login", json={"account": "admin", "password": "wrong-password"}
            ),
        )
        show(
            "账号不存在",
            await client.post(
                f"{prefix}/auth/login", json={"account": "nobody", "password": "wrong-password"}
            ),
        )
        show(
            "停用账号",
            await client.post(
                f"{prefix}/auth/login", json={"account": "guest", "password": "nacho-guest"}
            ),
        )
        show(
            "账号字符集不对",
            await client.post(
                f"{prefix}/auth/login", json={"account": "admin 中文", "password": "nacho-admin"}
            ),
        )
        show("路由不存在", await client.get(f"{prefix}/nope"))

        # 4) 令牌：正常 / 被改过 / 会话被吊销
        print("[4] 令牌怎么用：")
        show("带令牌取当前用户", await client.get(
            f"{prefix}/auth/me", headers={"Authorization": f"Bearer {token}"}
        ))
        show("没带令牌", await client.get(f"{prefix}/auth/me"))
        show("令牌被改过", await client.get(
            f"{prefix}/auth/me", headers={"Authorization": f"Bearer {token[:-2]}xy"}
        ))
        # 令牌是有状态的：登录设备的列表就是这些会话，吊销一条，旧令牌立刻失效
        show("我开着的登录", await client.get(
            url=f"{prefix}/auth/sessions", headers={"Authorization": f"Bearer {token}"}
        ))
        row = await client.get(
            f"{prefix}/auth/sessions", headers={"Authorization": f"Bearer {token}"}
        )
        token_hash = row.json()["data"][0]["token_hash"]  # 令牌摘要：这条登录的 id
        show("吊销这条登录", await client.delete(
            f"{prefix}/auth/sessions/{token_hash}",
            headers={"Authorization": f"Bearer {token}"},
        ))
        show("再用旧令牌", await client.get(
            f"{prefix}/auth/me", headers={"Authorization": f"Bearer {token}"}
        ))

        # 5) trace_id：请求头带什么，响应头 / 响应体 / 日志里就是什么
        print("[5] trace_id 串起来：")
        traced = await client.post(
            f"{prefix}/auth/login", json=ADMIN, headers={"X-Trace-Id": "demo-trace-1"}
        )
        print(f"      请求头 X-Trace-Id=demo-trace-1 -> 响应头 {traced.headers['X-Trace-Id']}"
              f" / 响应体 trace_id={traced.json()['trace_id']}")

    # 6) 看日志：写日志只是入队，等一轮分发再 flush，落盘的才全
    await asyncio.sleep(0.2)
    await core.flush()
    lines: list[str] = [line for line in LOG_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    print(f"[6] {LOG_PATH.name} 共 {len(lines)} 行，最后 3 行：")
    for line in lines[-3:]:
        print(f"      {line}")

    await manager.stop()

    # 7) 起真服务（这里只给命令）
    print("[7] 对外服务用这条命令起：")
    print("      uvicorn examples.api_demo:app --port 18080   # 或把 create_app 单独放一个模块")
    print("      # POST http://127.0.0.1:18080/api/auth/login")


#: ``uvicorn examples.api_demo:app`` 要用：模块级的应用实例（选项取 config.toml 的 [api]）。
#: 注意日志：这样直接起服务时日志核心没被 ``start``，只进队列不落盘；要落盘就照
#: :func:`main` 里那三步来（configure -> start -> attach_api_logging）。
app = create_app(demo_options())


if __name__ == "__main__":
    asyncio.run(main())
