"""執行中的整個教會：每個牧區一個 BotService，共用一份教會資料庫。

網頁、排程、LINE Webhook、指令列都透過 ``Church`` 拿到「某個牧區的 BotService」，
BotService 本身完全不知道有別的牧區——它只看自己資料夾裡的設定和名單（見 config.Paths）。

* ``root``：整個教會那一層的 BotService。它的資料庫就是共用的那一份（LINE 用量、對外網址、
  機器人被邀進去但還沒分到任何牧區的群組）；它沒有自己的群組，所以不會發任何提醒。
* 一個 LINE 群組屬於哪個牧區，看哪個牧區的「LINE 群組」清單裡有它（``owner_of``）。
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from dataclasses import dataclass, replace

from church_bot.church import ChurchConfig, Ministry, load_church, migrate_legacy
from church_bot.config import Paths
from church_bot.core import versions
from church_bot.core.history import History
from church_bot.errors import ChurchBotError, ConfigError
from church_bot.models import Member, Target
from church_bot.service import BotService
from church_bot.tables import TABLE_WRITE_LOCK, MemberTable, TargetTable

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Unit:
    """一個牧區 + 它的 BotService。"""

    ministry: Ministry
    service: BotService

    @property
    def id(self) -> str:
        return self.ministry.id

    @property
    def name(self) -> str:
        return self.ministry.name

    def targets(self) -> list[Target]:
        try:
            return TargetTable(self.service.paths.targets_file).load().items
        except ChurchBotError as exc:
            log.error("讀「%s」的 LINE 群組失敗：%s", self.name, exc)
            return []

    def members(self) -> list[Member]:
        try:
            return MemberTable(self.service.paths.members_file).load().items
        except ChurchBotError as exc:
            log.error("讀「%s」的同工名單失敗：%s", self.name, exc)
            return []


class Church:
    def __init__(self, paths: Paths, *, migrate: bool = True) -> None:
        self.paths = paths.church
        self.versions = versions.track(self.paths.root, self.paths.shared_db_file)  # 之後每次存檔都留紀錄
        if migrate:
            with versions.source("升級成多牧區"):
                migrate_legacy(self.paths)  # 舊版單一牧區：第一次開新版時自動搬家
        self.root = BotService(self.paths)
        self._services: dict[str, BotService] = {}
        self._lock = threading.Lock()

    @property
    def shared(self) -> History:
        return self.root.history

    def config(self) -> ChurchConfig:
        return load_church(self.paths)

    def ministries(self) -> list[Ministry]:
        return self.config().ministries

    def service(self, ministry_id: str) -> BotService:
        with self._lock:
            if ministry_id not in self._services:
                self._services[ministry_id] = BotService(self.paths.for_ministry(ministry_id), shared=self.shared)
            return self._services[ministry_id]

    def unit(self, key: str) -> Unit | None:
        """用編號或名稱找牧區。"""
        ministry = self.config().find(key)
        return Unit(ministry, self.service(ministry.id)) if ministry else None

    def require(self, key: str) -> Unit:
        unit = self.unit(key)
        if unit is None:
            names = "、".join(m.name for m in self.ministries()) or "（還沒有任何牧區）"
            raise ConfigError(f"找不到牧區「{key}」", f"現有的牧區：{names}")
        return unit

    def units(self) -> list[Unit]:
        return [Unit(m, self.service(m.id)) for m in self.ministries()]

    # ------------------------------------------------------------------ 本月 LINE 用量（整個帳號共用一份）

    def _quota_unit(self) -> Unit | None:
        """由哪個牧區的設定去問 LINE 用量：第一個「正式發送、有開額度檢查」的牧區。都是測試模式就不問。"""
        return next((u for u in self.units() if u.service.quota_status() is not None), None)

    def refresh_quota(self, *, force: bool = False):  # noqa: ANN201 - QuotaSnapshot | None
        unit = self._quota_unit()
        return unit.service.refresh_quota(force=force) if unit else None

    def quota_status(self):  # noqa: ANN201 - QuotaSnapshot | None
        unit = self._quota_unit()
        return unit.service.quota_status() if unit else None

    # ------------------------------------------------------------------ LINE 群組、人 → 哪個牧區

    def owner_of(self, chat_id: str) -> Unit | None:
        """這個群組（或私訊的人）在哪個牧區的「LINE 群組」清單裡。有啟用的優先；都沒有就 None。"""
        if not chat_id:
            return None
        fallback: Unit | None = None
        for unit in self.units():
            for target in unit.targets():
                if target.line_id != chat_id:
                    continue
                if target.enabled:
                    return unit
                fallback = fallback or unit
        return fallback

    def units_of_person(self, user_id: str) -> list[Unit]:
        """這個人跟哪些牧區有關：同工名單對應到他，或在那個牧區的群組裡講過話。私訊機器人時用來判斷要記在哪裡。"""
        if not user_id:
            return []
        return [u for u in self.units()
                if any(m.line_user_id == user_id for m in u.members()) or u.service.history.person(user_id)]

    def admin_units(self, user_id: str) -> list[Unit]:
        """這個人在哪些牧區是管理員（同工名單勾了管理員，或是那個牧區設定的「出問題通知誰」）。"""
        from church_bot.config import load_settings

        hits = []
        for unit in self.units():
            try:
                explicit = load_settings(unit.service.paths).line.admin_target_id.strip()
            except ChurchBotError:
                explicit = ""
            if user_id and (user_id == explicit or any(m.admin and m.line_user_id == user_id for m in unit.members())):
                hits.append(unit)
        return hits

    # ------------------------------------------------------------------ 還沒分配的群組

    def unassigned_chats(self) -> list[dict]:
        """機器人被邀進去、但還沒放進任何牧區的群組（首頁會列出來，點一下分到某個牧區）。"""
        owned = {t.line_id for unit in self.units() for t in unit.targets()}
        return [c for c in self.shared.chats()
                if c["status"] == "active" and c["kind"] in ("group", "room") and c["chat_id"] not in owned]

    def assign_chat(self, chat_id: str, ministry_id: str, name: str = "") -> Target:
        """把一個群組分到某個牧區：加進那個牧區的「LINE 群組」（先不啟用），在裡面收集到的人也一起交過去。"""
        unit = self.require(ministry_id)
        if (owner := self.owner_of(chat_id)) is not None:
            raise ConfigError(f"這個群組已經在「{owner.name}」裡了", "要換牧區，先到那個牧區的「LINE 群組」把它刪掉。")
        chat = self.shared.chat(chat_id) or {}
        today = dt.date.today()
        target = Target(name=name.strip() or chat.get("name") or f"新群組 {today:%m/%d}", line_id=chat_id, enabled=False,
                        note=f"{today:%m/%d} 從「還沒分配的群組」加入，確認後勾選「要收到提醒」")
        with TABLE_WRITE_LOCK:
            table = TargetTable(unit.service.paths.targets_file)
            items = table.load().items if table.path.exists() else []
            if any(t.name == target.name for t in items):  # 名稱撞到既有的群組：加上 ID 尾碼分開
                target = replace(target, name=f"{target.name}（{chat_id[-4:]}）")
            items.append(target)
            table.save(items)
        moved = self.shared.hand_over_chat(chat_id, unit.service.history)
        log.info("群組 %s 分到「%s」，一起交過去 %d 個 LINE 帳號", chat_id, unit.name, moved)
        return target
