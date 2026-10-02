"""HTTP 请求节点：发一次 HTTP 请求，把状态码与响应正文从两个输出端口送给下游。

config（``url`` / ``body`` 也能被连线覆盖：接到同名入口就用线上的值）:
    url:      请求地址（必填：接线或手填）
    method:   请求方法（必填，GET / POST / PUT / PATCH / DELETE / HEAD / OPTIONS）
    headers:  请求头（可选，dict；只能手写在 config 里 —— 没有对应端口）
    body:     请求体（可选）。要发 JSON 就写一段 JSON 串，再加 ``Content-Type: application/json``
    timeout:  超时秒数（可选，缺省 10；给 0 或负数表示不超时）

输出端口（下游把线连到这些端口就拿到值）：

    ``http_status``  响应状态码（int）
    ``http_body``    响应正文（str）

**两种失败是分开的**（故意的）：

* HTTP **4xx / 5xx** 是「对方的回答」= **业务失败**：抛
  :class:`~botnode.workflow.nodes.base.NodeFailure` —— 引擎**停止它向下传播**（下游整段跳过），
  不再把错误状态码当正常结果往下送（状态码与正文在日志里照样看得见）；
* **连不上 / 超时 / DNS 失败**是环境问题：直接抛出去，整条流程失败并留下堆栈，不让它伪装成
  一次「成功但没内容」的执行。

依赖 ``httpx``（可选依赖，``pip install "botnode[workflow]"``）：没装时报错并给安装提示，
不影响其它节点。
"""
from __future__ import annotations

from typing import Any, cast

from ..models import ValidationIssue, WorkflowNode
from .base import (
    TRIGGER_PORT,
    ConfigField,
    NodeExecutionContext,
    NodeFailure,
    PortSpec,
    input_value,
)
from .registry import register_node

#: 允许的请求方法（大写），**顺序即画布下拉顺序**
HTTP_METHOD_ORDER: tuple[str, ...] = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")

#: 允许的请求方法集合（校验规则与执行器认同这一份；与上面的顺序表同一份内容）
HTTP_METHODS: frozenset[str] = frozenset(HTTP_METHOD_ORDER)

#: 缺省超时（秒）
DEFAULT_TIMEOUT: float = 10.0


def validate_http_node(node: WorkflowNode) -> list[ValidationIssue]:
    """method 拼错在校验阶段就拦住（方法名是固定枚举，没有「运行期才知道」的说法）。"""
    method = node.config.get("method")
    if isinstance(method, str) and method.strip() and method.strip().upper() not in HTTP_METHODS:
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
        hint = 'HTTP 节点需要 httpx：pip install httpx（或 pip install "botnode[workflow]"）'
        raise RuntimeError(hint) from exc
    return httpx


def _plain_headers(raw: object) -> dict[str, str]:
    """把 ``config.headers`` 收敛成请求头：值统一转字符串（手写的 dict，没有模板替换）。"""
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in cast("dict[object, object]", raw).items()}


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
    label="HTTP",
    order=60,
    category="action",
    # url / body 既是字段名也是数据入口：上游把值接到这两个端口，就覆盖 config 里手填的内容
    inputs=[
        TRIGGER_PORT,
        PortSpec("url", "message", "请求地址", required=True),
        PortSpec("body", "message", "请求体"),
    ],
    outputs=[
        TRIGGER_PORT,
        PortSpec("http_status", "message", "状态码"),
        PortSpec("http_body", "message", "响应正文"),
    ],
    fields=[
        # url 的「必填」由入口（PortSpec.required）管：接线或手填都行，两个都没有才报错
        ConfigField("url", "请求地址"),
        # method 缺省 GET（画布一直这么填，这里把它写成后端的事实）；显式给空串仍会被
        # required 拦住，拼错则由 validate_http_node 报 INVALID_HTTP_METHOD
        ConfigField(
            "method", "请求方法", required=True, default="GET", options=HTTP_METHOD_ORDER
        ),
        ConfigField("body", "请求体"),
        ConfigField("timeout", "超时秒数", default=DEFAULT_TIMEOUT),
    ],
    validator=validate_http_node,
)
async def exec_http(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """HTTP 请求节点：发一次请求，产出 ``http_status`` / ``http_body`` 两个端口的值。"""
    method = str(node.config.get("method", "")).strip().upper()
    if method not in HTTP_METHODS:
        raise ValueError(
            f"节点 {node.id} 的 method 不合法：{method!r}（可选 {', '.join(sorted(HTTP_METHODS))}）"
        )
    url = str(input_value(node, ctx, "url", default="")).strip()
    if not url:
        raise ValueError(f"节点 {node.id} 的 url 为空（入口没接线，config 里也没填）")

    headers = _plain_headers(node.config.get("headers"))
    body = str(input_value(node, ctx, "body", default=""))
    timeout = _timeout_of(node.config.get("timeout"))

    httpx = _import_httpx()
    ctx.logger.info("HTTP 请求", node_id=node.id, method=method, url=url)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(method, url, headers=headers, content=body or None)
    except httpx.HTTPError as exc:
        # 连不上 / 超时 / DNS / 协议错：环境问题，直接抛 —— 但把方法、地址、超时与异常
        # **类型 + repr** 写进消息（httpx 的 str(exc) 常常是空串，光靠它排不了错）
        detail = str(exc) or repr(exc)
        raise ConnectionError(
            f"[http:{node.id}] {method} {url} 请求失败：{type(exc).__name__}: {detail}"
            f"（timeout={timeout}）"
        ) from exc

    text: str = response.text
    report = ctx.logger.warning if response.status_code >= 400 else ctx.logger.info
    report(
        f"[http:{node.id}] {method} {url} -> {response.status_code}",
        node_id=node.id,
        bytes=len(text),
    )
    ctx.log.append(
        f"[http] {node.id}: {method} {url} -> {response.status_code}（{len(text)} 字节）"
    )
    if response.status_code >= 400:
        # 4xx / 5xx 是「对方的回答」= 业务失败：停止向下传播，别把错误状态码当正常结果往下送
        raise NodeFailure(f"HTTP {response.status_code}（{method} {url}）")
    return {"http_status": response.status_code, "http_body": text}
