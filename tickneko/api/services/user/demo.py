"""演示账号：只有这一份，由 :meth:`SqlUserStore.seed_demo` 落库。

单独拎出来是为了**单一事实来源**，改一个账号只改一处，不会漂移。

形状是 ``(账号, 明文密码, 昵称, 角色, 是否停用)``；密码在这里是明文，由哈希器现算成哈希
再落存储，**明文不进存储层**。

只种一个 ``admin / tickneko-admin``（管理员）：够本地试用了，也少几份「默认口令」要人记得改。
要别的账号走 ``/api/auth/register`` 注册（或管理入口建），演示数据里不再带了。
"""
from __future__ import annotations

DEMO_USERS: tuple[tuple[str, str, str, tuple[str, ...], bool], ...] = (
    ("admin", "tickneko-admin", "管理员", ("admin", "user"), False),
)
