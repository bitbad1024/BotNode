"""机器人管理服务（``BotManager``）：把凭证落库 + 平台适配器生命周期统一成一套操作。

接口层（``nacho.api``）只认 :mod:`nacho.api.api.bots.protocols` 里的 ``BotsService`` 协议，
由本模块的实现**结构化满足**：这样「Kook 的启停要 start / stop 正向 WS 客户端」这类平台
细节被封在这里，接口层不用 import 任何平台包。

本模块坐在几点之上（装配层注入）：

* ``store`` —— :class:`nacho.bots.SqlBotStore`：凭证的落库 CRUD（多实例、跨平台）；
* ``onebot`` —— :class:`nacho.platforms.bridge.onebot.OneBotAdapter`：OneBot 的在线列表与启停 /
  吊销（复用服务端「断开连接」那一套）；
* ``kook`` —— :class:`nacho.platforms.bridge.kook.KookAdapter`：Kook 的客户端生命周期（多客户端）；
* ``secret_key`` —— Kook Bot Token 落库加密的密钥（签发时加密、连接时解密）。

平台分歧怎么收敛：**每个平台一个生命周期对象**（:class:`BotLifecycle`，见下）——OneBot
的「增」= 签发随机令牌（反向 WS 握手按它认归属，就一个 ``store.issue``），「停用 / 删」
要走 ``OneBotAdapter.set_token_enabled`` / ``revoke_by_id`` 以便断开正连着的客户端；Kook
的「增」= 存 Bot Token（可逆加密）+ 起正向 WS 客户端，「停用 / 删」= 停 / 删那个客户端。
``BotManager`` 按 ``platform`` 查表派发给对应 lifecycle，接口层看到的是同一套
「增 / 启停 / 删」；加第三个平台 = 写一个 lifecycle + 注册一行，本类四个方法一个不改。
"""
from __future__ import annotations

from typing import Protocol

from nacho.bots import SqlBotStore
from nacho.core.logger import BaseLogger, default_core

from .kook import KookAdapter
from .models import BotClient
from .onebot import OneBotAdapter


class BotLifecycle(Protocol):
    """一个平台的「管理面生命周期」：增 / 启停 / 删 / 在线的平台侧动作。

    每个平台一个实现（``manager.py`` 里 OneBot / Kook 各一份），持有该平台适配器
    （可为 ``None`` = 没接该平台）与必要的 store / 密钥，把「平台怎么落库、怎么起停
    客户端、在线按什么口径取」封在自己内部。协调器（``BotManager``）只认这个形状、
    不认平台。
    """

    @property
    def platform(self) -> str:
        """平台标识（路由键；与适配器的 ``platform`` 一致）。"""
        ...

    async def issue(
        self,
        owner_id: str,
        *,
        account: str = "",
        remark: str = "",
        token: str | None = None,
    ):
        """给 ``owner_id`` 签一个机器人（含平台侧接线）；返回签发结果。"""
        ...

    async def set_enabled(self, record, enabled: bool) -> bool:
        """启用 / 停用一条记录；真改到了返回 ``True``。"""
        ...

    async def revoke_by_id(self, record) -> bool:
        """删除一条记录（吊销凭证并断开 / 停掉客户端）；真删掉了返回 ``True``。"""
        ...

    def online_clients(self, record) -> tuple[BotClient, ...]:
        """这一个机器人此刻挂在线的客户端快照；没在线是空。"""
        ...


class _StoreOnlyLifecycle:
    """兜底生命周期：平台没注册 / 适配器没接时，只做落库开关、不起客户端。

    「增」照常落库（OneBot 随机令牌 / Kook 自填 Token 由 store 分平台处理）；
    「启停 / 删」只改记录；「在线」恒空。
    """

    def __init__(self, platform: str, store: SqlBotStore, *, secret_key: str = "") -> None:
        self._platform: str = platform
        self._store: SqlBotStore = store
        self._secret_key: str = secret_key

    @property
    def platform(self) -> str:
        return self._platform

    async def issue(
        self,
        owner_id: str,
        *,
        account: str = "",
        remark: str = "",
        token: str | None = None,
    ):
        return await self._store.issue(
            owner_id,
            platform=self._platform,  # type: ignore[arg-type]
            account=account,
            remark=remark,
            token=token,
            secret_key=self._secret_key,
        )

    async def set_enabled(self, record, enabled: bool) -> bool:
        return await self._store.set_enabled(record.bot_id, enabled)

    async def revoke_by_id(self, record) -> bool:
        return await self._store.remove_by_id(record.bot_id)

    def online_clients(self, record) -> tuple[BotClient, ...]:
        return ()


class _OneBotLifecycle:
    """OneBot：签发随机令牌（反向 WS 握手按它认归属）；启停 / 删走服务端断开连接。"""

    def __init__(self, store: SqlBotStore, adapter: OneBotAdapter | None) -> None:
        self._store: SqlBotStore = store
        self._adapter: OneBotAdapter | None = adapter

    @property
    def platform(self) -> str:
        return "onebot"

    async def issue(
        self,
        owner_id: str,
        *,
        account: str = "",
        remark: str = "",
        token: str | None = None,
    ):
        # OneBot 的令牌是系统随机签发的，用户不填（force None）
        return await self._store.issue(
            owner_id, platform="onebot", account=account, remark=remark, token=None
        )

    async def set_enabled(self, record, enabled: bool) -> bool:
        if self._adapter is not None:
            return await self._adapter.set_token_enabled(record.bot_id, enabled)
        return await self._store.set_enabled(record.bot_id, enabled)

    async def revoke_by_id(self, record) -> bool:
        if self._adapter is not None:
            return await self._adapter.revoke_by_id(record.bot_id)
        return await self._store.remove_by_id(record.bot_id)

    def online_clients(self, record) -> tuple[BotClient, ...]:
        if self._adapter is None:
            return ()
        return self._adapter.clients(owner_id=record.owner_id)


class _KookLifecycle:
    """Kook：存用户自填的 Bot Token（可逆加密）+ 管正向 WS 客户端（多客户端）。"""

    def __init__(
        self,
        store: SqlBotStore,
        adapter: KookAdapter | None,
        *,
        secret_key: str,
    ) -> None:
        self._store: SqlBotStore = store
        self._adapter: KookAdapter | None = adapter
        self._secret_key: str = secret_key

    @property
    def platform(self) -> str:
        return "kook"

    async def issue(
        self,
        owner_id: str,
        *,
        account: str = "",
        remark: str = "",
        token: str | None = None,
    ):
        issued = await self._store.issue(
            owner_id,
            platform="kook",
            account=account,
            remark=remark,
            token=token,
            secret_key=self._secret_key,
        )
        if self._adapter is not None:
            await self._ensure_client(issued.record.bot_id)
            await self._adapter.start_bot(issued.record.bot_id)
        return issued

    async def set_enabled(self, record, enabled: bool) -> bool:
        changed = await self._store.set_enabled(record.bot_id, enabled)
        if changed and self._adapter is not None:
            if enabled:
                await self._ensure_client(record.bot_id)
                await self._adapter.start_bot(record.bot_id)
            else:
                await self._adapter.stop_bot(record.bot_id)
        return changed

    async def revoke_by_id(self, record) -> bool:
        removed = await self._store.remove_by_id(record.bot_id)
        if removed and self._adapter is not None:
            await self._adapter.remove_bot(record.bot_id)
        return removed

    def online_clients(self, record) -> tuple[BotClient, ...]:
        if self._adapter is None:
            return ()
        # Kook 的 clients() 过滤键是 bot_id（record.id），不是归属用户
        return self._adapter.clients(owner_id=record.id)

    async def _ensure_client(self, bot_id: str) -> None:
        """保证 Kook 适配器里有这个机器人的客户端：没有就解密 Bot Token 后登记。

        :raises ValueError: 没配加密密钥 / 解不开密文（理论上签发时已保证，这里兜底）。
        """
        assert self._adapter is not None
        if self._adapter.has_bot(bot_id):
            return
        token = await self._store.decrypt_token(bot_id, self._secret_key)
        if not token:
            raise ValueError(
                f"Kook 机器人 {bot_id} 的 Bot Token 解不出来（secret_key 对不对？）"
            )
        record = await self._store.get_by_id(bot_id)
        owner_id = record.owner_id if record is not None else ""
        self._adapter.add_bot(bot_id, token, owner_id=owner_id)


class BotManager:
    """跨平台机器人管理：凭证 CRUD + 适配器生命周期（按平台查表派发）。

    :param store: 机器人凭证存储（``SqlBotStore``，多实例跨平台）；
    :param onebot: OneBot 适配器（可空：没接 OneBot 时只做落库开关，不断连接）；
    :param kook: Kook 适配器（可空：没接 Kook 时只做落库开关，不起客户端）；
    :param secret_key: Kook Bot Token 落库加密的密钥。
    :param logger: 业务日志实例，默认 ``bridge`` 那个。
    """

    def __init__(
        self,
        store: SqlBotStore,
        *,
        onebot: OneBotAdapter | None = None,
        kook: KookAdapter | None = None,
        secret_key: str = "",
        logger: BaseLogger | None = None,
    ) -> None:
        self._store: SqlBotStore = store
        self._secret_key: str = secret_key
        self._log: BaseLogger = logger if logger is not None else default_core().child("bridge")
        #: platform -> 生命周期（没接的适配器也注册一份「只落库」的兜底）
        self._lifecycles: dict[str, BotLifecycle] = {}
        for lifecycle in (
            _OneBotLifecycle(store, onebot),
            _KookLifecycle(store, kook, secret_key=secret_key),
        ):
            self._lifecycles[lifecycle.platform] = lifecycle

    def _lifecycle(self, platform: str) -> BotLifecycle:
        """按平台查生命周期；没注册的平台退到只落库的兜底。"""
        return self._lifecycles.get(platform, _StoreOnlyLifecycle(platform, self._store, secret_key=self._secret_key))

    # ------------------------------------------------------------------ 查询
    async def list_records(self, *, owner_id: str | None = None) -> list:
        """列机器人（多实例）；给 ``owner_id`` 就只列那个归属下的。"""
        return await self._store.list_records(owner_id=owner_id)

    async def get_by_id(self, bot_id: str):
        """按记录主键（bot_id）取一条；找不到返回 ``None``。"""
        return await self._store.get_by_id(bot_id)

    def online_clients(self, record) -> tuple[BotClient, ...]:
        """这一个机器人此刻挂在线的客户端快照；没在线是空（口径交给平台）。"""
        return self._lifecycle(record.platform).online_clients(record)

    # ------------------------------------------------------------------ 增
    async def issue(
        self,
        owner_id: str,
        *,
        platform: str = "onebot",
        account: str = "",
        remark: str = "",
        token: str | None = None,
    ):
        """给 ``owner_id`` 签一个机器人（平台侧接线交给 lifecycle）。"""
        return await self._lifecycle(platform).issue(
            owner_id, account=account, remark=remark, token=token
        )

    # ------------------------------------------------------------------ 启停
    async def set_enabled(self, bot_id: str, enabled: bool) -> bool:
        """启用 / 停用一个机器人；真改到了返回 ``True``。"""
        record = await self._store.get_by_id(bot_id)
        if record is None:
            return False
        return await self._lifecycle(record.platform).set_enabled(record, enabled)

    # ------------------------------------------------------------------ 删
    async def remove_by_id(self, bot_id: str) -> bool:
        """删除一个机器人（吊销凭证并断开 / 停掉客户端）；真删掉了返回 ``True``。"""
        record = await self._store.get_by_id(bot_id)
        if record is None:
            return False
        return await self._lifecycle(record.platform).revoke_by_id(record)