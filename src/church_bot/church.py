"""牧區清單（``config/church.yaml``）：一個教會有很多牧區，每個牧區是一整套獨立的設定與名單。

為什麼這樣分（詳見 docs/MINISTRIES.md）：
* 每個牧區一個資料夾（``config/ministries/<編號>/``、``data/ministries/<編號>/``），裡面的檔案跟單一牧區時一模一樣，
  所以原本的程式（BotService、表格、網頁）幾乎不用改，只是換一個資料夾；搬移、備份、刪除一個牧區就是一個資料夾。
* 牧區編號（m1、m2…）建立後永遠不變，網址和指令列都用它；名稱隨時可以改。
* 這個檔案只記「有哪些牧區」和整個教會一份的東西（網頁 host/port）；牧區自己的設定在它的 settings.yaml。

第一次用新版開啟時，原本放在 ``config/`` 底下的單一牧區會自動搬進第一個牧區（見 ``migrate_legacy``）。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import logging
import re
import secrets
import shutil
import sqlite3
import threading
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

import yaml

from church_bot.config import Paths, Settings, load_settings, save_settings
from church_bot.errors import ConfigError
from church_bot.files import write_text

log = logging.getLogger(__name__)

CHURCH_LOCK = threading.RLock()
ID_RE = re.compile(r"^m\d+$")
NAME_MAX_LENGTH = 30
LEGACY_FILES = ("settings.yaml", "targets.csv", "members.csv", "teams.csv")
DEFAULT_FIRST_NAME = "第一個牧區"
_PBKDF2_ROUNDS = 200_000


# --------------------------------------------------------------------------- 第二層密碼（牧區自己的）


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ROUNDS).hex()
    return f"pbkdf2${_PBKDF2_ROUNDS}${salt}${digest}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, rounds, salt, digest = stored.split("$")
        fresh = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(rounds)).hex()
    except ValueError:
        return False
    return hmac.compare_digest(fresh, digest)


# --------------------------------------------------------------------------- church.yaml


@dataclass(frozen=True, slots=True)
class Ministry:
    id: str
    name: str
    note: str = ""  # 例如負責人：「王小明、林美華」
    password_hash: str = ""  # 第二層密碼；空白 = 這個牧區沒有另外設密碼

    @property
    def has_password(self) -> bool:
        return bool(self.password_hash)


@dataclass(slots=True)
class ChurchConfig:
    ministries: list[Ministry] = field(default_factory=list)
    web: dict = field(default_factory=dict)  # host、port：整個教會一份（見 config.load_settings）

    def get(self, ministry_id: str) -> Ministry | None:
        return next((m for m in self.ministries if m.id == ministry_id), None)

    def find(self, key: str) -> Ministry | None:
        """用編號或名稱找牧區（名稱不分全形半形、大小寫、空白）。"""
        key = (key or "").strip()
        if hit := self.get(_norm(key)):
            return hit
        wanted = _norm(key)
        return next((m for m in self.ministries if _norm(m.name) == wanted), None)

    def next_id(self) -> str:
        used = [int(m.id[1:]) for m in self.ministries if ID_RE.match(m.id)]
        return f"m{max(used, default=0) + 1}"


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or "")).casefold()


def load_church(paths: Paths) -> ChurchConfig:
    file = paths.church_file
    if not file.exists():
        return ChurchConfig()
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8-sig")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError("牧區清單 config/church.yaml 格式錯誤",
                          "可以用「變更紀錄」還原，或對照 docs/MINISTRIES.md 修正。") from exc
    if not isinstance(data, dict):
        raise ConfigError("牧區清單 config/church.yaml 內容不是正確格式", "對照 docs/MINISTRIES.md 修正。")
    ministries = []
    for raw in data.get("ministries") or []:
        if not isinstance(raw, dict) or not ID_RE.match(str(raw.get("id", ""))):
            log.warning("church.yaml 有一筆牧區看不懂，略過：%r", raw)
            continue
        ministries.append(Ministry(id=str(raw["id"]), name=str(raw.get("name") or raw["id"]),
                                   note=str(raw.get("note") or ""), password_hash=str(raw.get("password_hash") or "")))
    web = data.get("web") if isinstance(data.get("web"), dict) else {}
    return ChurchConfig(ministries, web)


def save_church(paths: Paths, cfg: ChurchConfig) -> None:
    data: dict = {"ministries": [
        {k: v for k, v in (("id", m.id), ("name", m.name), ("note", m.note), ("password_hash", m.password_hash)) if v}
        for m in cfg.ministries
    ]}
    if cfg.web:
        data["web"] = cfg.web
    header = ("# 教會服事提醒機器人 — 牧區清單（建議在管理網頁首頁修改）\n"
              "# 每個牧區的設定與名單在 config/ministries/<編號>/；編號建立後不要改\n\n")
    write_text(paths.church_file, header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100))


def update_church(paths: Paths, change: Callable[[ChurchConfig], None]) -> ChurchConfig:
    with CHURCH_LOCK:
        cfg = load_church(paths)
        change(cfg)
        save_church(paths, cfg)
    return cfg


def _clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name or "").strip()
    if not name:
        raise ConfigError("請填牧區名稱")
    if len(name) > NAME_MAX_LENGTH:
        raise ConfigError(f"牧區名稱太長了（最多 {NAME_MAX_LENGTH} 個字）")
    return name


def _check_unique(cfg: ChurchConfig, name: str, except_id: str = "") -> None:
    if any(_norm(m.name) == _norm(name) and m.id != except_id for m in cfg.ministries):
        raise ConfigError(f"已經有叫「{name}」的牧區了", "換一個名字，或直接點進那個牧區。")


def add_ministry(paths: Paths, name: str, note: str = "") -> Ministry:
    """新增一個牧區：建好資料夾、寫一份預設設定（測試模式還沒接服事表也不會亂發）。"""
    name = _clean_name(name)
    created: list[Ministry] = []

    def change(cfg: ChurchConfig) -> None:
        _check_unique(cfg, name)
        ministry = Ministry(id=cfg.next_id(), name=name, note=note.strip())
        mpaths = paths.for_ministry(ministry.id)
        mpaths.config_dir.mkdir(parents=True, exist_ok=True)
        mpaths.data_dir.mkdir(parents=True, exist_ok=True)
        if not mpaths.settings_file.exists():
            save_settings(mpaths, new_ministry_settings())
        cfg.ministries.append(ministry)
        created.append(ministry)

    update_church(paths, change)
    log.info("新增牧區：%s（%s）", created[0].name, created[0].id)
    return created[0]


def new_ministry_settings() -> Settings:
    """新牧區的預設設定：服事表還沒接、還沒有群組，自動發送先關著，等設定好再自己打開。"""
    settings = Settings()
    settings.source.kind = "google_public"
    settings.schedule.enabled = False
    return settings


def edit_ministry(paths: Paths, ministry_id: str, *, name: str | None = None, note: str | None = None,
                  password_hash: str | None = None) -> Ministry:
    result: list[Ministry] = []

    def change(cfg: ChurchConfig) -> None:
        current = cfg.get(ministry_id)
        if current is None:
            raise ConfigError(f"找不到牧區「{ministry_id}」")
        updated = current
        if name is not None:
            clean = _clean_name(name)
            _check_unique(cfg, clean, except_id=ministry_id)
            updated = replace(updated, name=clean)
        if note is not None:
            updated = replace(updated, note=note.strip())
        if password_hash is not None:
            updated = replace(updated, password_hash=password_hash)
        cfg.ministries = [updated if m.id == ministry_id else m for m in cfg.ministries]
        result.append(updated)

    update_church(paths, change)
    return result[0]


def remove_ministry(paths: Paths, ministry_id: str) -> Path:
    """把牧區從清單拿掉。資料夾不刪，整包移到 config/_deleted/（發送紀錄在 data/ 底下也留著），後悔可以搬回來。"""
    holder: list[Path] = []

    def change(cfg: ChurchConfig) -> None:
        ministry = cfg.get(ministry_id)
        if ministry is None:
            raise ConfigError(f"找不到牧區「{ministry_id}」")
        source = paths.for_ministry(ministry_id).config_dir
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = paths.root / "config" / "_deleted" / f"{ministry_id}-{stamp}"
        if source.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(dest))
        cfg.ministries = [m for m in cfg.ministries if m.id != ministry_id]
        holder.append(dest)

    update_church(paths, change)
    log.warning("已移除牧區 %s，資料夾移到 %s", ministry_id, holder[0])
    return holder[0]


# --------------------------------------------------------------------------- 從單一牧區升級


def has_legacy_files(paths: Paths) -> bool:
    church = paths.church
    return any((church.config_dir / name).exists() for name in LEGACY_FILES)


def migrate_legacy(paths: Paths, name: str = DEFAULT_FIRST_NAME) -> Ministry | None:
    """第一次用新版：把原本 config/ 底下的設定、三張表和 data/church_bot.db 搬進第一個牧區。

    * 已經有 church.yaml（搬過了），或根本沒有舊資料（全新安裝），就什麼都不做，回傳 None。
    * 原本的檔案另外留一份在 config/_before_ministries/、data/_before_ministries/，確認沒問題再刪。
    * 服事表 CSV、服務帳號金鑰這類用路徑指過去的檔案不搬（設定裡的路徑是相對專案根目錄，搬了反而找不到）。
    * 資料庫整份複製給牧區（發送紀錄、防重複、收集到的 LINE 帳號都跟著走）；
      教會那一份只留「還沒分到牧區的群組」和用量、網址這種整個帳號共用的東西。
    """
    church = paths.church
    with CHURCH_LOCK:
        if church.church_file.exists() or not (has_legacy_files(church) or church.shared_db_file.exists()):
            return None  # 已經搬過，或是全新安裝（首頁會請你新增第一個牧區）
        ministry = Ministry(id="m1", name=_clean_name(name))
        target = church.for_ministry(ministry.id)
        target.config_dir.mkdir(parents=True, exist_ok=True)
        target.data_dir.mkdir(parents=True, exist_ok=True)
        web = _legacy_web(church.settings_file)  # 網頁的 host/port 是整個教會一份，搬到 church.yaml
        backup = church.config_dir / "_before_ministries"
        for file_name in LEGACY_FILES:
            source = church.config_dir / file_name
            if source.exists():
                backup.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, backup / file_name)
                shutil.move(str(source), str(target.config_dir / file_name))
        db = church.shared_db_file
        if db.exists():
            data_backup = church.data_dir / "_before_ministries"
            data_backup.mkdir(parents=True, exist_ok=True)
            shutil.copy2(db, data_backup / db.name)
            shutil.copy2(db, target.db_file)
            _keep_only_shared(db)
        try:  # 重存一次：web（host/port）已經搬到 church.yaml，牧區的設定檔裡不再留一份
            save_settings(target, load_settings(target))
        except ConfigError:
            pass  # 設定檔本來就有錯：原封不動，畫面上會照常提示怎麼修
        save_church(church, ChurchConfig([ministry], web))
    log.warning("已升級成多牧區：原本的設定與名單搬進「%s」（%s），舊檔留在 config/_before_ministries/",
                ministry.name, ministry.id)
    return ministry


def _legacy_web(settings_file: Path) -> dict:
    if not settings_file.exists():
        return {}
    try:
        data = yaml.safe_load(settings_file.read_text(encoding="utf-8-sig")) or {}
    except yaml.YAMLError:
        return {}
    web = data.get("web") if isinstance(data, dict) else None
    return {k: web[k] for k in ("host", "port") if isinstance(web, dict) and k in web}


def _keep_only_shared(db: Path) -> None:
    """教會那一份資料庫：發送紀錄和收集到的人都已經跟著牧區走了，這裡清掉，免得兩邊各有一份越差越多。"""
    conn = sqlite3.connect(db)
    try:
        for table in ("runs", "deliveries", "people", "memberships"):
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                conn.execute(f"DELETE FROM {table}")  # noqa: S608 - 表名是上面固定的清單
        conn.commit()
    finally:
        conn.close()
