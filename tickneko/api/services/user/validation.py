"""用户模块自己的输入校验规则：账号长什么样、密码多长。

校验分两层，各管一段：

* **字段级**：账号、密码长什么样，写在这里用 ``Annotated`` 类型别名收口，请求模型直接
  引用（登录见 :mod:`tickneko.api.api.auth.requests`），pydantic 负责在解析请求体时跑它们；
* **消息级**：请求体整体缺字段、类型不对，由 FastAPI 的 ``RequestValidationError``
  接住，由接口层的异常处理器（:mod:`tickneko.api.common.errors.handlers`）翻成统一的错误结构。

规则本身放在这里是为了让「注册」「改资料」以后能复用同一份 —— 用户对账号 / 密码的要求
在哪都应该是同一套。

密码用 ``SecretStr`` 包着：调试输出、日志、异常回溯里它都只显示 ``'**********'``，
不会把明文带出去。
"""
from __future__ import annotations

import re
from typing import Annotated, Final, TypeAlias

from pydantic import AfterValidator, SecretStr

#: 账号长度与字符要求（登录、注册、改资料共用一份）
ACCOUNT_MIN_LENGTH: Final[int] = 3
ACCOUNT_MAX_LENGTH: Final[int] = 32
#: 密码长度要求；只限制长度，强度（要几种字符）交给业务自己加
PASSWORD_MIN_LENGTH: Final[int] = 8
PASSWORD_MAX_LENGTH: Final[int] = 128
#: 昵称长度要求（库里列宽 128，这里按「显示名」收得更紧；字数按字符数算，中文也算 1 个）
NICKNAME_MIN_LENGTH: Final[int] = 1
NICKNAME_MAX_LENGTH: Final[int] = 32

#: 账号允许的字符集：字母、数字、下划线、点、短横线
_ACCOUNT_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.-]+$")


def _check_account(value: str) -> str:
    """账号：先去两端空白，再查长度与字符集；返回清洗后的值。"""
    account: str = value.strip()
    if not account:
        raise ValueError("账号不能为空")
    if not ACCOUNT_MIN_LENGTH <= len(account) <= ACCOUNT_MAX_LENGTH:
        raise ValueError(
            f"账号长度要在 {ACCOUNT_MIN_LENGTH}-{ACCOUNT_MAX_LENGTH} 之间，收到 {len(account)}"
        )
    if not _ACCOUNT_PATTERN.match(account):
        raise ValueError("账号只能包含字母、数字、下划线、点与短横线")
    return account


def _check_password(value: SecretStr) -> SecretStr:
    """密码：只查长度，不查强度（强度是产品策略，写死在这儿会误伤）。"""
    raw: str = value.get_secret_value()
    if not PASSWORD_MIN_LENGTH <= len(raw) <= PASSWORD_MAX_LENGTH:
        raise ValueError(
            f"密码长度要在 {PASSWORD_MIN_LENGTH}-{PASSWORD_MAX_LENGTH} 之间，收到 {len(raw)}"
        )
    return value


def _check_nickname(value: str) -> str:
    """昵称：去两端空白后查长度；**不管字符集**（叫什么名字不该由正则来管）。"""
    nickname: str = value.strip()
    if not NICKNAME_MIN_LENGTH <= len(nickname) <= NICKNAME_MAX_LENGTH:
        raise ValueError(
            f"昵称长度要在 {NICKNAME_MIN_LENGTH}-{NICKNAME_MAX_LENGTH} 之间，收到 {len(nickname)}"
        )
    return nickname


#: 账号：去空白 + 长度 + 字符集
Account: TypeAlias = Annotated[str, AfterValidator(_check_account)]
#: 密码：``SecretStr`` + 长度
Password: TypeAlias = Annotated[SecretStr, AfterValidator(_check_password)]
#: 昵称：去空白 + 长度
Nickname: TypeAlias = Annotated[str, AfterValidator(_check_nickname)]
