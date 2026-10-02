"""出门的响应壳：成功的样子与失败的样子，**各只有一种**。

两头都不直接返回裸对象，而是套一层壳，好处是客户端不用先看状态码再决定怎么解析：

* 成功：``{"success": true, "data": {...}, "trace_id": "..."}``；
* 失败：``{"success": false, "error": {code, message, details}, "trace_id": "..."}``。

``trace_id`` 是这一次请求的编号（请求进来时由日志中间件生成，也写在响应头
``X-Trace-Id`` 上），排障时用户报这一个号就够查日志。

业务自己的响应体（登录结果、用户资料）不在这里 —— 入口层自己的响应模型在
:mod:`botnode.api.api.auth.responses`，填进 ``data``。这一份只管**外面那层壳**。
"""
from __future__ import annotations

from typing import ClassVar, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

#: 响应里 ``data`` 的类型参数：每个接口的载荷各不一样，壳是同一份
DataT = TypeVar("DataT")

#: 请求没经过日志中间件时（比如直接调服务层）用的占位编号
DEFAULT_TRACE_ID: str = "-"


class _Frozen(BaseModel):
    """响应模型的公共底：冻结（产出之后不该被谁改），并允许传任意字段进来。

    业务模块的模型都继承它，好处是「禁止改动」这条规矩只在这一处定义。
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)


class ApiResponse(_Frozen, Generic[DataT]):
    """统一的成功响应外壳：``data`` 里才是这一次要的结果。"""

    success: Literal[True] = True
    data: DataT
    trace_id: str = DEFAULT_TRACE_ID


# --------------------------------------------------------------------------- 错误
class ErrorDetail(_Frozen):
    """一项具体错误：哪一字段、为什么（校验失败时才有）。"""

    #: 出错的位置，如 ``body.account``；整条请求的问题（缺字段）就是空串
    field: str = ""
    message: str


class ErrorPayload(_Frozen):
    """错误本体：机器看 ``code``、人看 ``message``，细节看 ``details``。"""

    code: str = Field(description="错误码，见 botnode.api.ErrorCode")
    message: str = Field(description="给人看的一句话")
    details: list[ErrorDetail] = Field(default_factory=list)


class ErrorResponse(_Frozen):
    """统一的失败响应：``success`` 恒为 ``false``，错误在 ``error`` 里。"""

    success: Literal[False] = False
    error: ErrorPayload
    trace_id: str = DEFAULT_TRACE_ID
