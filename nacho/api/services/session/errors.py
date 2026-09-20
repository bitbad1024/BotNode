"""会话域自己会抛的错（**不认识 HTTP**）。

只有一个：令牌摘要撞了。它属于**存储层**的错，两个实现都得抛同一个类型，因为
:class:`~nacho.api.services.session.protocols.SessionStore` 是一份协议——同一件事在
两个实现里表现不同，协议就没定住。

撞的概率极低（256 位输出的生日界，见
:func:`~nacho.api.services.session.tokens.hash_token`），这个类型的存在**不是为了处理它，
而是为了不静默出错**：撞上时必须响亮地失败，绝不覆盖。
"""
from __future__ import annotations


class TokenHashCollisionError(Exception):
    """令牌摘要已经存在（这该是撞不上的）。

    落库版撞的是 ``auth_sessions.token_hash`` 的主键，内存版自己比一眼字典。

    **绝不允许覆盖**：覆盖写等于把 A 那条登录的记录换成 B 的（设备列表里就变成了别人
    的设备），而令牌本身又不可逆、没有第二处能发现这件事。宁可这一次登录失败。

    它**不是** :class:`~nacho.api.common.errors.ApiError`：进来了说明这是服务端的 bug，
    没有任何客户端输入能让它发生。所以让它一路冒到全局处理器，回 500 并**带上堆栈**
    进日志——这正是这种错该有的待遇。
    """

    def __init__(self, token_hash: str) -> None:
        #: 撞上的那个摘要（sha256 十六进制，泄不出去也不怕）
        self.token_hash: str = token_hash
        super().__init__(f"令牌摘要已存在，拒绝覆盖：{token_hash}")
