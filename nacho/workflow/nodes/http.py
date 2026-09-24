"""HTTP 请求节点：发一次 HTTP 请求，把状态码与响应正文交给下游。

config:
    url:      请求地址（必填，可含 ``{{变量}}``）
    method:   请求方法（必填，GET / POST / PUT / PATCH / DELETE / HEAD / OPTIONS）
    headers:  请求头（可选，dict；里面的字符串同样支持 ``{{变量}}``）
    body:     请求体（可选，字符串，支持 ``{{变量}}``）。要发 JSON 就写一段 JSON 串，再加
              ``Content-Type: application/json``
    timeout:  超时秒数（可选，缺省 10；给 0 或负数表示不超时）

输出（下游用 ``{{名字}}`` 引用，记得在节点的 ``outputs`` 里声明）：

    ``http_status``  响应状态码（int）
    ``http_body``    响应正文（str）

**两种失败是分开的**（故意的）：

* HTTP **4xx / 5xx** 是「对方的回答」，不算异常：记一条 warning，状态码与正文照常交给下游
  —— 将来接上条件节点就能按 ``{{http_status}}`` 分流；
* **连不上 / 超时 / DNS 失败**是环境问题：直接抛出去，整条流程失败并留下堆栈，不让它伪装成
  一次「成功但没内容」的执行。

依赖 ``httpx``（可选依赖，``pip install "nacho[workflow]"``）：没装时报错并给安装提示，
不影响其它节点。
"""
from __future__ import annotations

import re
from typing import Any, cast

from ..models import ValidationIssue, WorkflowNode
from .base import ConfigField, NodeExecutionContext, render_variables
from .registry import register_node

#: 允许的请求方法（大写）。校验规则与执行器认同这一份
HTTP_METHODS: frozenset[str] = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
)

#: 缺省超时（秒）
DEFAULT_TIMEOUT: float = 10.0

#: {{变量}}：method 写成模板时运行期才渲染得出，静态校验放行
_VARIABLE_RE = re.compile(r"\{\{\s*[A-Za-z_][A-Za-z0-9_]*\s*\}\}")


def validate_http_node(node: WorkflowNode) -> list[ValidationIssue]:
    """method 拼错在校验阶段就拦住；写成 ``{{变量}}`` 的放行（运行期才知道）。"""
    method = node.config.get("method")
    if (
        isinstance(method, str)
        and method.strip()
        and _VARIABLE_RE.search(method) is None
        and method.strip().upper() not in HTTP_METHODS
    ):
        return [
            ValidationIssue(
                node_id=node.id,
                code="INVALID_HTTP_METHOD",
                message=f"http 节点 {node.id} 的方法 {method!r} 不合法",
                suggestion=f"可选方法：{', '.join(sorted(HTTP_METHODS))}",
            )
        ]
    return []


def _import_httpx() -> Any:
    """取 httpx 模块；没装就抛错并给安装提示（可选依赖，见模块文档）。

    返回 ``Any``：``httpx`` 是可选依赖，装不装都可能，标注成具体类型反而要求它一定在。
    """
    try:
        import httpx
    except ImportError as exc:
        hint = 'HTTP 节点需要 httpx：pip install httpx（或 pip install "nacho[workflow]"）'
        raise RuntimeError(hint) from exc
    return httpx


def _render_headers(raw: object, variables: dict[str, Any]) -> dict[str, str]:
    """把 ``config.headers`` 渲染成请求头：值统一转字符串，字符串里支持 ``{{变量}}``。"""
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): render_variables(str(value), variables)
        for key, value in cast("dict[object, object]", raw).items()
    }


def _timeout_of(raw: object) -> float | None:
    """超时秒数：缺省 :data:`DEFAULT_TIMEOUT`；给 0 或负数表示不超时（httpx 认 None）。"""
    if isinstance(raw, (int, float, str)):
        try:
            seconds = float(raw)
        except ValueError:  # 字符串但不像数：当没写
            return DEFAULT_TIMEOUT
        return None if seconds <= 0 else seconds
    return DEFAULT_TIMEOUT


@register_node(
    "http",
    fields=[
        ConfigField("url", "请求地址", required=True),
        ConfigField("method", "请求方法", required=True),
        ConfigField("timeout", "超时秒数", default=DEFAULT_TIMEOUT),
    ],
    validator=validate_http_node,
)
async def exec_http(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """HTTP 请求节点：按配置发一次请求，返回 ``http_status`` / ``http_body``。"""
    method = render_variables(str(node.config.get("method", "")), ctx.variables).strip().upper()
    if method not in HTTP_METHODS:
        raise ValueError(
            f"节点 {node.id} 的 method 不合法：{method!r}（可选 {', '.join(sorted(HTTP_METHODS))}）"
        )
    url = render_variables(str(node.config.get("url", "")), ctx.variables).strip()
    if not url:
        raise ValueError(f"节点 {node.id} 的 url 为空（渲染之后）")

    headers = _render_headers(node.config.get("headers"), ctx.variables)
    body = render_variables(str(node.config.get("body", "")), ctx.variables)
    timeout = _timeout_of(node.config.get("timeout"))

    httpx = _import_httpx()
    ctx.logger.info("HTTP 请求", node_id=node.id, method=method, url=url)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.request(method, url, headers=headers, content=body or None)

    text: str = response.text
    # 4xx/5xx 只是「对方的回答」：记 warning 并照常交给下游（连不上那种才抛，见模块文档）
    report = ctx.logger.warning if response.status_code >= 400 else ctx.logger.info
    report(
        f"[http:{node.id}] {method} {url} -> {response.status_code}",
        node_id=node.id,
        bytes=len(text),
    )
    ctx.log.append(
        f"[http] {node.id}: {method} {url} -> {response.status_code}（{len(text)} 字节）"
    )
    return {"http_status": response.status_code, "http_body": text}
