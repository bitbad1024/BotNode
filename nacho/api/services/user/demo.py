"""演示账号：只有这一份，由 :meth:`SqlUserStore.seed_demo` 落库。

单独拎出来是为了**单一事实来源**，改一个账号只改一处，不会漂移。

形状是 ``(账号, 明文密码, 昵称, 角色, 是否停用)``；密码在这里是明文，由哈希器现算成哈希
再落存储，**明文不进存储层**。

:meth:`~nacho.api.services.user.store_sql.SqlUserStore.seed_demo` 造三个固定账号：
``admin / nacho-admin``（管理员）、``robot / nacho-robot``、``guest / nacho-guest``（已停用）。
"""
from __future__ import annotations

DEMO_USERS: tuple[tuple[str, str, str, tuple[str, ...], bool], ...] = (
    ("admin", "nacho-admin", "管理员", ("admin", "user"), False),
    ("robot", "nacho-robot", "巡检机器人", ("user",), False),
    ("guest", "nacho-guest", "停用账号", ("user",), True),
)
