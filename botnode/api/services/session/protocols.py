"""会话存储要向外要的那块能力（协议，只声明不实现）。

存的是一次登录一行：**主键就是令牌摘要**（所以不需要另起一套编号），外加设备信息。
参数一律叫 ``token_hash``，就是那个摘要本身。

实现只有一个：:class:`~botnode.api.services.session.store_sql.SqlSessionStore`（落库）。想要
「内存版」不必再写一份——把它的引擎指到内存 sqlite 就行（测试与不接库的兜底都这么用）。
"""
from __future__ import annotations

from typing import Protocol

from .models import ClientInfo, SessionRecord


class SessionStore(Protocol):
    """会话表（``auth_sessions``）上的操作。"""

    async def create(
        self,
        token_hash: str,
        user_id: str,
        *,
        client: ClientInfo,
        remembered: bool,
    ) -> SessionRecord:
        """开一条会话（登录时一行）。

        :param token_hash: **令牌摘要**，由调用方算好（服务端只认摘要，不认明文）。
        :raises TokenHashCollisionError: 这个摘要已经有记录了。**实现必须抛它，
            绝不能覆盖**——覆盖写等于把别人那条登录顶掉（见
            :class:`~botnode.api.services.session.errors.TokenHashCollisionError`）。
        """
        ...

    async def get(self, token_hash: str) -> SessionRecord | None:
        """按令牌摘要取；没有返回 ``None``。"""
        ...

    async def set_remembered(self, token_hash: str, *, remembered: bool) -> SessionRecord | None:
        """改这条会话「记住设备」的标记，返回**改完的那条记录**；没有这行返回 ``None``。

        登录复用旧令牌时会变（这次勾的和当初那次可能不一样）。它**只服务「登录设备」列表
        那一栏的显示**——真正的滑动有效期活在缓存的值里（见
        :class:`~botnode.api.services.session.tokens.TokenIndex`），跟这个字段无关。
        """
        ...

    async def remove(self, token_hash: str) -> bool:
        """删掉一条会话；真删掉了返回 ``True``。"""
        ...

    async def remove_all(self, user_id: str) -> int:
        """删掉这个用户的全部会话，返回删了几条（「全部下线」用）。"""
        ...

    async def list_for_user(self, user_id: str) -> tuple[SessionRecord, ...]:
        """这个用户的全部会话（新的在前），给「登录设备」列表用。"""
        ...
