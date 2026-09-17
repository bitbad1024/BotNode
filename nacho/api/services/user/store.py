""":class:`~nacho.api.services.user.protocols.UserStore` 的默认实现：内存字典。

账号 / id 各建一份索引，两个查询都是 O(1)。它不是给用户管理用的（没有增删改接口），
真要落库由数据库层照同一份协议另写一份实现，从 :func:`nacho.api.create_app` 传进来。

:meth:`InMemoryUserStore.demo` 造三个固定账号，示例与测试都用它::

    admin / nacho-admin     管理员，roles = ("admin", "user")
    robot / nacho-robot     普通用户，roles = ("user",)
    guest / nacho-guest     已停用（登录会被拒，用来看 ACCOUNT_DISABLED）

账号**区分大小写**，按原样匹配。
"""
from __future__ import annotations

from collections.abc import Iterable

from .models import UserRecord
from .protocols import PasswordHasher

#: 演示账号：账号 -> (密码, 昵称, 角色, 是否停用)
_DEMO_USERS: tuple[tuple[str, str, str, tuple[str, ...], bool], ...] = (
    ("admin", "nacho-admin", "管理员", ("admin", "user"), False),
    ("robot", "nacho-robot", "巡检机器人", ("user",), False),
    ("guest", "nacho-guest", "停用账号", ("user",), True),
)


class InMemoryUserStore:
    """把用户放在内存字典里：账号一份索引、id 一份索引。"""

    def __init__(self, users: Iterable[UserRecord] = ()) -> None:
        self._by_id: dict[str, UserRecord] = {}
        self._by_account: dict[str, UserRecord] = {}
        for user in users:
            self._by_id[user.id] = user
            self._by_account[user.account] = user

    def __len__(self) -> int:
        return len(self._by_id)

    async def get_by_account(self, account: str) -> UserRecord | None:
        """按账号取用户；没这个人返回 ``None``。"""
        return self._by_account.get(account)

    async def get_by_id(self, user_id: str) -> UserRecord | None:
        """按 id 取用户；没这个人返回 ``None``。"""
        return self._by_id.get(user_id)

    def add(
        self,
        account: str,
        password: str,
        *,
        hasher: PasswordHasher,
        user_id: str | None = None,
        nickname: str = "",
        roles: Iterable[str] = (),
        disabled: bool = False,
    ) -> UserRecord:
        """塞一个用户进去：明文密码在这里就被换成哈希，之后不再保留。

        账号重复直接抛 :class:`ValueError`（宁可早失败，也别悄悄覆盖掉一个账号）。
        """
        if account in self._by_account:
            raise ValueError(f"账号已存在：{account!r}")
        record = UserRecord(
            id=user_id or f"u-{len(self._by_id) + 1:04d}",
            account=account,
            password_hash=hasher.hash(password),
            nickname=nickname,
            roles=tuple(roles),
            disabled=disabled,
        )
        self._by_id[record.id] = record
        self._by_account[record.account] = record
        return record

    @classmethod
    def demo(cls, hasher: PasswordHasher) -> "InMemoryUserStore":
        """造一份带演示账号的存储（三个固定账号，见本模块文档）。"""
        store = cls()
        for account, password, nickname, roles, disabled in _DEMO_USERS:
            store.add(
                account,
                password,
                hasher=hasher,
                nickname=nickname,
                roles=roles,
                disabled=disabled,
            )
        return store
