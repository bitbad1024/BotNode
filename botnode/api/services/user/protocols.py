"""用户模块要向外要的两块能力（协议）。

这里只声明「需要什么」，**不实现**：真要落库或者用别的算法，由调用方（入口 ``app.py`` /
示例）把实现塞进 :func:`botnode.api.create_app` 就行，本模块一行都不用改。

两份协议是同一件事的两半 —— 「用户这一块数据怎么来」::

    UserStore       人到哪查（按账号找：登录时用；按 id 找：验令牌时用）
    PasswordHasher  密码怎么算（存之前 hash，登录时 verify）

:class:`UserStore` 是**异步**的：接口层跑在事件循环里，查库这种阻塞动作迟早要 ``await``，
现在就把协议定成异步，换成真实实现时不用改调用方。

不要求继承（结构化子类型）：实现只要有同名同签名的方法就算数，第三方自己的用户类也
能直接传进来。
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from .models import UserRecord


@runtime_checkable
class UserStore(Protocol):
    """用户存储：登录时按账号找人，验令牌时按 id 找人。"""

    async def get_by_account(self, account: str) -> UserRecord | None:
        """按账号取用户；没有就返回 ``None``（不抛，账号存不存在是正常分支）。"""
        ...

    async def get_by_id(self, user_id: str) -> UserRecord | None:
        """按用户 id 取用户（令牌里带的是 id）；没有返回 ``None``。"""
        ...

    async def get_by_ids(self, user_ids: Iterable[str]) -> dict[str, UserRecord]:
        """按一批 id 取用户，返回 ``id -> 记录``；不在库里的那些 id 就不出现在结果里。

        列表页（比如 OneBot 令牌列表要显示归属昵称）用它**一次查齐**，别一条一条查（N+1）。
        """
        ...

    async def list_all(self) -> list[UserRecord]:
        """列出**全部**用户（管理视角出「归属」清单用：哪些人手里有内容）。

        管理员的工作流 / 机器人列表要按归属筛选，选项就照它渲染；普通用户问「有哪些归属」
        的答案只有他自己，接口层照这个口径给。按账号排序，顺序稳定（不该随插入顺序抖）。
        """
        ...

    async def add(
        self, *, account: str, password_hash: str, nickname: str = ""
    ) -> UserRecord:
        """新增一个账号（注册用），返回落库后的记录。

        ``password_hash`` 是**已经算好**的哈希（明文密码不进这一层）；``id`` 由实现自己生成。

        账号已被占用时抛
        :class:`~botnode.api.common.errors.AccountAlreadyExistsError`：实现要在写库时兜住唯一约束，
        并发下两个请求同时注册同一个账号，出来的也得是这个异常，而不是数据库的冲突错误。
        """
        ...

    async def set_nickname(self, user_id: str, nickname: str) -> UserRecord | None:
        """改昵称（个人设置那一笔），返回改完的记录；用户不存在返回 ``None``。

        只动 ``nickname`` 一列：密码将来走「改密码」、角色与停用走管理入口，别从「改资料」
        这儿顺手改掉别的东西。
        """
        ...


@runtime_checkable
class PasswordHasher(Protocol):
    """密码哈希：存进去之前先 ``hash``，登录时拿用户输入 ``verify``。

    ``verify`` 只回答 True / False，**不抛异常**：账号不存在、密码不对在调用方看来是
    同一件事（不告诉对方账号到底存不存在）。
    """

    def hash(self, password: str) -> str:
        """把明文密码变成可以落库的字符串（自带盐与参数，能原样存）。"""
        ...

    def verify(self, password: str, hashed: str) -> bool:
        """比对明文与库里那份哈希；哈希串坏了 / 算法不认就返回 False。"""
        ...
