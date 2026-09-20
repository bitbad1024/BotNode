"""会话存储的内存实现：不接数据库时用（测试 / 演示），进程重启即失效。"""
from __future__ import annotations

import time
from dataclasses import replace

from .errors import TokenHashCollisionError
from .models import ClientInfo, SessionRecord


class InMemorySessionStore:
    """会话放内存字典：``令牌摘要 -> SessionRecord``。"""

    def __init__(self) -> None:
        self._rows: dict[str, SessionRecord] = {}

    async def create(
        self,
        token_hash: str,
        user_id: str,
        *,
        client: ClientInfo,
        remembered: bool,
    ) -> SessionRecord:
        if token_hash in self._rows:
            # 字典赋值是**覆盖写**，撞了就会悄悄把别人那条登录换掉——绝不这么干。
            # 落库版有主键兜底，这一份没有约束，所以只能自己看一眼。
            raise TokenHashCollisionError(token_hash)
        record = SessionRecord(
            token_hash=token_hash,
            user_id=user_id,
            created_at=time.time(),
            remembered=remembered,
            ip=client.ip,
            device_name=client.device_name,
            device_type=client.device_type,
            browser=client.browser,
            os=client.os,
            user_agent=client.user_agent,
        )
        self._rows[record.token_hash] = record
        return record

    async def get(self, token_hash: str) -> SessionRecord | None:
        return self._rows.get(token_hash)

    async def set_remembered(self, token_hash: str, *, remembered: bool) -> SessionRecord | None:
        existing = self._rows.get(token_hash)
        if existing is None:
            return None
        # SessionRecord 是 frozen：改字段得换一个新对象（dataclasses.replace 正合适）
        updated = replace(existing, remembered=remembered)
        self._rows[token_hash] = updated
        return updated

    async def remove(self, token_hash: str) -> bool:
        return self._rows.pop(token_hash, None) is not None

    async def remove_all(self, user_id: str) -> int:
        doomed = [key for key, row in self._rows.items() if row.user_id == user_id]
        for key in doomed:
            del self._rows[key]
        return len(doomed)

    async def list_for_user(self, user_id: str) -> tuple[SessionRecord, ...]:
        rows = [row for row in self._rows.values() if row.user_id == user_id]
        return tuple(sorted(rows, key=lambda row: row.created_at, reverse=True))
