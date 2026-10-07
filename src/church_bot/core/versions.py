"""變更紀錄：每次存設定、名單、群組，都記下「什麼時候、從哪裡、改了什麼」，而且可以還原。

做法跟 git 一樣的想法（比較過 git、DoltLite，見 docs/MINISTRIES.md）：
* 每個版本的內容用 SHA-256 當名字存一份（``blobs``），**相同內容只存一份**，再用 zlib 壓縮；
  一份名單幾 KB，一年改幾百次也只有幾 MB，不用清。
* 每一次修改是一筆 ``changes``：哪個檔案、改之前是哪個版本、改之後是哪個版本、從哪裡改的、一句話摘要。
* 有人直接用 Excel／記事本改檔案（不經過程式）：下次程式存檔前會發現「檔案跟上次記的不一樣」，
  先補一筆「直接改檔案」，所以那次修改一樣看得到、也能還原。
* 刻意**不記是誰改的**（擁有者決定），只記來源：管理網頁、LINE 指令、機器人自己、指令列、還原。

所有寫檔都走 files.write_text，那裡會呼叫 ``record``；只記 config/ 底下的設定和三張表、牧區清單。
資料放在整個教會共用的資料庫（data/church_bot.db），移除牧區之後紀錄也還在。
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime as dt
import difflib
import hashlib
import io
import csv
import logging
import sqlite3
import threading
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import yaml

log = logging.getLogger(__name__)

TRACKED = {"settings.yaml": "設定", "targets.csv": "LINE 群組", "members.csv": "同工名單", "teams.csv": "小團",
           "church.yaml": "牧區清單"}
SECTION_ZH = {"source": "服事表來源", "schedule": "什麼時候發", "message": "訊息", "behavior": "發送行為",
              "chat": "LINE 聊天室指令", "line": "出問題通知誰", "messenger": "發送方式", "web": "網站",
              "ministries": "牧區"}
EXTERNAL = "直接改檔案（Excel／記事本）"
BASELINE = "開始記錄前的內容"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS blobs (
    hash TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    data BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    scope TEXT NOT NULL,          -- 牧區編號；空字串 = 整個教會（church.yaml）
    file TEXT NOT NULL,           -- 相對專案資料夾的路徑，例如 config/ministries/m1/targets.csv
    source TEXT NOT NULL,
    summary TEXT NOT NULL,
    before TEXT,                  -- 改之前的版本（NULL = 原本沒有這個檔案）
    after TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_changes_scope ON changes(scope, id);
CREATE INDEX IF NOT EXISTS idx_changes_file ON changes(file, id);
"""

_source: contextvars.ContextVar[str] = contextvars.ContextVar("change_source", default="程式")


@contextlib.contextmanager
def source(label: str) -> Iterator[None]:
    """這段程式裡的存檔，紀錄上的「來源」寫 label（例如「管理網頁」「LINE 指令」）。"""
    token = _source.set(label)
    try:
        yield
    finally:
        _source.reset(token)


def set_source(label: str) -> contextvars.Token:
    return _source.set(label)


def reset_source(token: contextvars.Token) -> None:
    _source.reset(token)


@dataclass(frozen=True, slots=True)
class Change:
    id: int
    at: str
    scope: str
    file: str
    source: str
    summary: str
    before: str | None
    after: str

    @property
    def label(self) -> str:
        return TRACKED.get(Path(self.file).name, Path(self.file).name)

    @property
    def when(self) -> str:
        return self.at[:16].replace("T", " ").replace("-", "/")


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


class VersionStore:
    def __init__(self, root: Path, db_path: Path) -> None:
        self.root = root.resolve()
        self.db_path = db_path
        self._lock = threading.Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextlib.contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.db_path, timeout=10)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------ 記錄

    def where(self, path: Path) -> tuple[str, str] | None:
        """這個檔案要不要記：要的話回傳 (scope, 相對路徑)。只記 config/ 底下的設定、三張表和牧區清單。"""
        try:
            rel = path.resolve().relative_to(self.root)
        except ValueError:
            return None
        parts = rel.parts
        if not parts or parts[0] != "config" or rel.name not in TRACKED:
            return None
        if len(parts) == 4 and parts[1] == "ministries":
            return parts[2], rel.as_posix()
        if len(parts) == 2:
            return "", rel.as_posix()
        return None

    def record(self, path: Path, before: bytes | None, after: bytes) -> None:
        place = self.where(path)
        if place is None or before == after:
            return
        scope, rel = place
        try:
            with self._conn() as conn:
                last = conn.execute("SELECT after FROM changes WHERE file=? ORDER BY id DESC LIMIT 1", (rel,)).fetchone()
                if before is not None and (last is None or last["after"] != _hash(before)):
                    # 上次記的跟檔案現在不一樣（或從來沒記過）：先補一筆，那次的內容才還原得回來
                    label = EXTERNAL if last is not None else BASELINE
                    self._insert(conn, scope, rel, label, last["after"] if last else None, before,
                                 previous=self._blob(conn, last["after"]) if last else None)
                self._insert(conn, scope, rel, _source.get(), _hash(before) if before is not None else None, after,
                             previous=before)
        except sqlite3.Error:
            log.exception("寫變更紀錄失敗（檔案已經存好了，只是這次沒有留紀錄）")

    def _insert(self, conn: sqlite3.Connection, scope: str, rel: str, src: str, before_hash: str | None,
                after: bytes, previous: bytes | None) -> None:
        after_hash = _hash(after)
        conn.execute("INSERT OR IGNORE INTO blobs (hash, size, data) VALUES (?,?,?)",
                     (after_hash, len(after), zlib.compress(after, 9)))
        summary = summarize(Path(rel).name, previous, after) if src != BASELINE else "記下原本的內容"
        conn.execute("INSERT INTO changes (at, scope, file, source, summary, before, after) VALUES (?,?,?,?,?,?,?)",
                     (_now(), scope, rel, src, summary, before_hash, after_hash))

    @staticmethod
    def _blob(conn: sqlite3.Connection, digest: str | None) -> bytes | None:
        if digest is None:
            return None
        row = conn.execute("SELECT data FROM blobs WHERE hash=?", (digest,)).fetchone()
        return zlib.decompress(row["data"]) if row else None

    # ------------------------------------------------------------------ 查詢、還原

    def changes(self, scope: str, limit: int = 200) -> list[Change]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM changes WHERE scope=? ORDER BY id DESC LIMIT ?", (scope, limit)).fetchall()
        return [Change(**dict(r)) for r in rows]

    def change(self, change_id: int) -> Change | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM changes WHERE id=?", (change_id,)).fetchone()
        return Change(**dict(row)) if row else None

    def content(self, digest: str | None) -> bytes | None:
        with self._conn() as conn:
            return self._blob(conn, digest)

    def diff(self, change: Change, context: int = 2) -> list[tuple[str, str]]:
        """(種類, 那一行)：種類是 add / del / ctx / hunk。給畫面一行一行上色。"""
        before = _text(self.content(change.before))
        after = _text(self.content(change.after))
        lines: list[tuple[str, str]] = []
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=context):
            if line.startswith(("---", "+++")):
                continue
            kind = "hunk" if line.startswith("@@") else "add" if line.startswith("+") else \
                "del" if line.startswith("-") else "ctx"
            lines.append((kind, line if kind == "hunk" else line[1:]))
        return lines


def _text(data: bytes | None) -> str:
    if not data:
        return ""
    text = data.decode("utf-8-sig", errors="replace").replace("\r\n", "\n")
    return "\n".join(_mask_line(line) for line in text.split("\n"))


def _mask_line(line: str) -> str:
    """牧區密碼的雜湊不要出現在畫面上。"""
    return line.split(":")[0] + ": （牧區密碼，已隱藏）" if line.strip().startswith("password_hash:") else line


# --------------------------------------------------------------------------- 一句話摘要


def summarize(name: str, before: bytes | None, after: bytes) -> str:
    try:
        if before is None:
            return "建立"
        if name.endswith(".csv"):
            return _summarize_csv(before, after)
        return _summarize_yaml(before, after)
    except Exception:  # noqa: BLE001 - 摘要只是方便看，算不出來也不能影響存檔
        return "修改"


def _rows(data: bytes) -> dict[str, list[str]]:
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig", errors="replace"))))
    return {r[0].strip(): r for r in rows[1:] if r and r[0].strip()}


def _summarize_csv(before: bytes, after: bytes) -> str:
    old, new = _rows(before), _rows(after)
    added = [k for k in new if k not in old]
    removed = [k for k in old if k not in new]
    changed = [k for k in new if k in old and new[k] != old[k]]
    parts = [f"新增「{'、'.join(added[:3])}」" + (f"等 {len(added)} 筆" if len(added) > 3 else "")] if added else []
    parts += [f"刪除「{'、'.join(removed[:3])}」" + (f"等 {len(removed)} 筆" if len(removed) > 3 else "")] if removed else []
    parts += [f"修改「{'、'.join(changed[:3])}」" + (f"等 {len(changed)} 筆" if len(changed) > 3 else "")] if changed else []
    return "；".join(parts) or "調整欄位或順序"


def _flatten(value: object, prefix: str = "") -> dict[str, str]:
    if isinstance(value, dict):
        out: dict[str, str] = {}
        for k, v in value.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
        return out
    if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
        out = {}
        for item in value:  # church.yaml 的牧區清單：用編號當名字
            key = str(item.get("id", len(out)))
            out.update(_flatten(item, f"{prefix}.{key}"))
        return out
    return {prefix: "" if value is None else str(value)}


def _summarize_yaml(before: bytes, after: bytes) -> str:
    old = _flatten(yaml.safe_load(before.decode("utf-8-sig")) or {})
    new = _flatten(yaml.safe_load(after.decode("utf-8-sig")) or {})
    keys = [k for k in dict.fromkeys([*old, *new]) if old.get(k) != new.get(k)]
    parts = []
    for key in keys[:4]:
        head, _, rest = key.partition(".")
        label = f"{SECTION_ZH.get(head, head)} › {rest}" if rest else SECTION_ZH.get(head, head)
        if key.endswith("password_hash"):
            parts.append(f"{label}：（牧區密碼變更）")
            continue
        was, now = old.get(key, "（沒有）"), new.get(key, "（沒有）")
        short = lambda s: (s[:24] + "…") if len(s) > 25 else s  # noqa: E731
        parts.append(f"{label}：{short(was)} → {short(now)}")
    if len(keys) > 4:
        parts.append(f"還有 {len(keys) - 4} 項")
    return "；".join(parts) or "格式調整"


# --------------------------------------------------------------------------- 哪個專案資料夾要記

_stores: dict[Path, VersionStore] = {}
_stores_lock = threading.Lock()


def track(root: Path, db_path: Path) -> VersionStore:
    """開始記錄這個專案資料夾（Church 建立時呼叫）。同一個資料夾只會有一個。"""
    key = root.resolve()
    with _stores_lock:
        if key not in _stores:
            _stores[key] = VersionStore(key, db_path)
        return _stores[key]


def store_for(path: Path) -> VersionStore | None:
    resolved = path.resolve()
    with _stores_lock:
        stores = list(_stores.values())
    return next((s for s in stores if resolved.is_relative_to(s.root)), None)
