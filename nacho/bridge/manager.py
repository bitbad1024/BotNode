"""机器人管理服务（``BotManager``）：把凭证落库 + 平台适配器生命周期统一成一套操作。

接口层（``nacho.api``）只认 :mod:`nacho.api.api.bots.protocols` 里的 ``BotsService`` 协议，
由本模块的实现**结构化满足**：这样「Kook 的启停要 start / stop 正向 WS 客户端」这类平台
细节被封在这里，接口层不用 import 任何平台包。

本模块坐在几点之上（装配层注入）：

* ``store`` —— :class:`nacho.bots.SqlBotStore`：凭证的落库 CRUD（多实例、跨平台）；
* ``onebot`` —— :class:`nacho.bridge.onebot.OneBotAdapter`：OneBot 的在线列表与启停 /
  吊销（复用服务端「断开连接」那一套）；
* ``kook`` —— :class:`nacho.bridge.kook.KookAdapter`：Kook 的客户端生命周期（多客户端）；
* ``secret_key`` —— Kook Bot Token 落库加密的密钥（签发时加密、连接时解密）。

平台分歧怎么收敛：OneBot 的「增」= 签发随机令牌（反向 WS 握手按它认归属，就一个
``store.issue``），「停用 / 删」要走 ``OneBotAdapter.set_token_enabled`` /
``revoke_by_id`` 以便断开正连着的客户端；Kook 的「增」= 存 Bot Token（可逆加密）+ 起
正向 WS 客户端，「停用 / 删」= 停 / 删那个客户端。两者在这里分派，接口层看到的是同一套
「增 / 启停 / 删」。
"""
from __future__ import annotations

from nacho.bots import SqlBotStore
from nacho.core.logger import BaseLogger, get_logger

from .kook import KookAdapter
from .models import BotClient
from .onebot import OneBotAdapter


class BotManager:
    """跨平台机器人管理：凭证 CRUD + 适配器生命周期。

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
        self._onebot: OneBotAdapter | None = onebot
        self._kook: KookAdapter | None = kook
        self._secret_key: str = secret_key
        self._log: BaseLogger = logger if logger is not None else get_logger("bridge")

    # ------------------------------------------------------------------ 查询
    async def list_records(self, *, owner_id: str | None = None) -> list:
        """列机器人（多实例）；给 ``owner_id`` 就只列那个归属下的。"""
        return await self._store.list_records(owner_id=owner_id)

    async def get_by_id(self, bot_id: str):
        """按记录主键（bot_id）取一条；找不到返回 ``None``。"""
        return await self._store.get_by_id(bot_id)

    def online_clients(self, record) -> tuple[BotClient, ...]:
        """这一个机器人此刻挂在线的客户端快照；没在线是空。

        OneBot 按归属（owner_id）取、Kook 按 bot_id 取——在线列表两边的口径各自成立。
        """
        if record.platform == "kook":
            adapter = self._kook
            return adapter.clients(owner_id=record.id) if adapter is not None else ()
        adapter = self._onebot
        return adapter.clients(owner_id=record.owner_id) if adapter is not None else ()

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
        """给 ``owner_id`` 签一个机器人：OneBot 自动生成令牌；Kook 用自填的 Bot Token（加密落库）。"""
        if platform == "onebot":
            token = None  # OneBot 反向 WS 的令牌是随机签发的，用户不填
        issued = await self._store.issue(
            owner_id,
            platform=platform,
            account=account,
            remark=remark,
            token=token,
            secret_key=self._secret_key,
        )
        if platform == "kook" and self._kook is not None:
            await self._ensure_kook_client(issued.record.bot_id)
            await self._kook.start_bot(issued.record.bot_id)
        return issued

    # ------------------------------------------------------------------ 启停
    async def set_enabled(self, bot_id: str, enabled: bool) -> bool:
        """启用 / 停用一个机器人；真改到了返回 ``True``。"""
        record = await self._store.get_by_id(bot_id)
        if record is None:
            return False
        if record.platform == "onebot":
            if self._onebot is not None:
                return await self._onebot.set_token_enabled(bot_id, enabled)
            return await self._store.set_enabled(bot_id, enabled)
        # kook
        changed = await self._store.set_enabled(bot_id, enabled)
        if changed and self._kook is not None:
            if enabled:
                await self._ensure_kook_client(bot_id)
                await self._kook.start_bot(bot_id)
            else:
                await self._kook.stop_bot(bot_id)
        return changed

    # ------------------------------------------------------------------ 删
    async def remove_by_id(self, bot_id: str) -> bool:
        """删除一个机器人（吊销凭证并断开 / 停掉客户端）；真删掉了返回 ``True``。"""
        record = await self._store.get_by_id(bot_id)
        if record is None:
            return False
        if record.platform == "onebot":
            if self._onebot is not None:
                return await self._onebot.revoke_by_id(bot_id)
            return await self._store.remove_by_id(bot_id)
        removed = await self._store.remove_by_id(bot_id)
        if removed and self._kook is not None:
            await self._kook.remove_bot(bot_id)
        return removed

    # ------------------------------------------------------------------ 内部
    async def _ensure_kook_client(self, bot_id: str) -> None:
        """保证 Kook 适配器里有这个机器人的客户端：没有就解密 Bot Token 后登记。

        :raises ValueError: 没配加密密钥 / 解不开密文（理论上签发时已保证，这里兜底）。
        """
        assert self._kook is not None
        if self._kook.has_bot(bot_id):
            return
        token = await self._store.decrypt_token(bot_id, self._secret_key)
        if not token:
            raise ValueError(
                f"Kook 机器人 {bot_id} 的 Bot Token 解不出来（secret_key 对不对？）"
            )
        self._kook.add_bot(bot_id, token)