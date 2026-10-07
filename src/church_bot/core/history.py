"""SQLite 紀錄：每次執行的結果、每則訊息送出的狀態（防重複發送）、LINE 回報的群組與人，
以及幾格「跨程式共用的小狀態」（state 表：本月 LINE 用量快照、目前的對外網址）。

資料檔在 data/church_bot.db。刪掉它不會壞，只是會忘記「送過什麼」、自動收集到的 LINE 帳號和歷史紀錄。
每個操作都開新連線 + 鎖：網頁和排程在不同執行緒，這樣最單純也最安全。
舊版建立的資料庫在開啟時會自動補上新欄位（見 ``_MIGRATIONS``），不用手動處理。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from church_bot.models import TRIGGER_ZH, DeliveryStatus, RunReport

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    trigger TEXT NOT NULL,
    dry_run INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    service_date TEXT,
    sent INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    warnings INTEGER NOT NULL DEFAULT 0,
    report_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);

CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    target_name TEXT NOT NULL,
    service_date TEXT,
    label TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deliveries_key ON deliveries(target_id, service_date, label, status);

CREATE TABLE IF NOT EXISTS chats (
    chat_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS people (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    chat_id TEXT NOT NULL DEFAULT '',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memberships (
    user_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (user_id, chat_id)
);

-- 一格一件小狀態（JSON）：本月 LINE 用量快照、目前的對外網址…
-- 管理網頁、cli.bat send、免費模式是三個不同的程式，放這裡誰更新的另一邊下次就看得到。
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# (資料表, 欄位, 欄位定義)：舊資料庫缺的欄位開啟時自動補上
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("people", "real_name", "TEXT NOT NULL DEFAULT ''"),  # 本人用「/我的名字」登記的名字，等管理員確認
    ("people", "real_name_at", "TEXT NOT NULL DEFAULT ''"),
    ("people", "nicknames", "TEXT NOT NULL DEFAULT ''"),  # 本人用「/我的暱稱」登記的稱呼（JSON 陣列），等管理員確認
    ("people", "ignored", "INTEGER NOT NULL DEFAULT 0"),  # 管理員按了「忽略」
    ("people", "profile_checked_at", "TEXT NOT NULL DEFAULT ''"),  # 上次向 LINE 查顯示名稱的時間
    ("chats", "member_count", "INTEGER"),
    ("chats", "count_checked_at", "TEXT NOT NULL DEFAULT ''"),
)


REPLY_COVERS = dt.timedelta(days=2)  # 「/提醒」送過多久以內，排程時間到了內容沒變就不再 Push
MAX_NICKNAMES = 5  # 一個人最多登記幾個暱稱（避免有人一直亂打）


def nicknames_of(person: dict | None) -> tuple[str, ...]:
    """把 people.nicknames 欄位（JSON 陣列）讀成 tuple；壞掉或空的都回傳空 tuple。"""
    try:
        values = json.loads((person or {}).get("nicknames") or "[]")
    except ValueError:
        return ()
    if not isinstance(values, list):
        return ()
    return tuple(str(v).strip() for v in values if str(v).strip())


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _json_default(obj: object) -> str:
    if isinstance(obj, (dt.date, dt.datetime)):
        return obj.isoformat()
    return str(obj)


@dataclass(frozen=True, slots=True)
class RunSummary:
    id: str
    trigger: str
    dry_run: bool
    started_at: str
    finished_at: str | None
    status: str
    service_date: str | None
    sent: int
    failed: int
    skipped: int
    errors: int
    warnings: int

    @property
    def status_zh(self) -> str:
        return {"error": "有錯誤", "warning": "有提醒事項", "ok": "正常"}.get(self.status, self.status)

    @property
    def trigger_zh(self) -> str:
        return TRIGGER_ZH.get(self.trigger, self.trigger)


class History:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            for table, column, ddl in _MIGRATIONS:
                existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            # 舊版只記「最後在哪個群組看到」；搬進 memberships，之後才知道每個人在哪些群組
            conn.execute(
                "INSERT OR IGNORE INTO memberships (user_id, chat_id, first_seen, last_seen) "
                "SELECT user_id, chat_id, first_seen, last_seen FROM people "
                "WHERE substr(chat_id, 1, 1) IN ('C', 'R')"
            )

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.db_path, timeout=10)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------ state（小狀態，見 _SCHEMA）

    def get_state(self, key: str) -> dict:
        """讀一格小狀態。沒存過、或內容壞掉都回傳空 dict（呼叫的人就當成「還不知道」）。"""
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        if row is None:
            return {}
        try:
            value = json.loads(row["value"])
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    def set_state(self, key: str, value: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO state (key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, json.dumps(value, ensure_ascii=False, default=_json_default), _now()),
            )

    # ------------------------------------------------------------------ dedup

    def skip_reason(self, target_id: str, service_date: dt.date, label: str, fp: str,
                    resend_if_changed: bool, now: dt.datetime | None = None) -> str | None:
        """回傳「為什麼這則不用再送」；None 代表要送。

        排程 Push 送過的一律算數；有人打「/提醒」（免費 Reply）送過的，只有在 REPLY_COVERS 之內才算
        ——週一有人打 /提醒 看一下，週四的排程還是要照常提醒大家；前一天晚上才打的，週四就不用再送。
        """
        now = now or dt.datetime.now().astimezone()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT d.fingerprint, d.created_at, r.trigger FROM deliveries d LEFT JOIN runs r ON r.id = d.run_id "
                "WHERE d.target_id=? AND d.service_date=? AND d.label=? AND d.status=? ORDER BY d.id DESC",
                (target_id, service_date.isoformat(), label, DeliveryStatus.SENT.value),
            ).fetchall()
        row = next((r for r in rows if r["trigger"] != "reply"
                    or now - dt.datetime.fromisoformat(r["created_at"]) <= REPLY_COVERS), None)
        if row is None:
            return None
        if resend_if_changed and row["fingerprint"] != fp:
            return None
        via = "（有人打 /提醒）" if row["trigger"] == "reply" else ""
        return f"{row['created_at'][:16].replace('T', ' ')} 已經送過了{via}"

    # ------------------------------------------------------------------ runs

    def record(self, report: RunReport) -> None:
        payload = json.dumps(dataclasses.asdict(report), ensure_ascii=False, default=_json_default)
        errors = sum(1 for i in report.issues if i.severity.value == "error")
        warnings = sum(1 for i in report.issues if i.severity.value == "warning")
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    report.run_id, report.trigger, int(report.dry_run),
                    report.started_at.isoformat(timespec="seconds"),
                    report.finished_at.isoformat(timespec="seconds") if report.finished_at else None,
                    report.status, report.service_date.isoformat() if report.service_date else None,
                    report.count(DeliveryStatus.SENT), report.count(DeliveryStatus.FAILED),
                    report.count(DeliveryStatus.SKIPPED), errors, warnings, payload,
                ),
            )
            conn.executemany(
                "INSERT INTO deliveries (run_id, target_id, target_name, service_date, label, fingerprint, status,"
                " detail, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (report.run_id, d.target_id, d.target_name,
                     d.service_date.isoformat() if d.service_date else None, d.label, d.fingerprint,
                     d.status.value, d.detail, _now())
                    for d in report.deliveries
                    if d.status is not DeliveryStatus.DRY_RUN
                ],
            )

    def recent_runs(self, limit: int = 30, include_previews: bool = False) -> list[RunSummary]:
        where = "" if include_previews else "WHERE dry_run = 0"
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM runs {where} ORDER BY started_at DESC LIMIT ?", (limit,)  # noqa: S608
            ).fetchall()
        return [self._summary(r) for r in rows]

    def last_run(self, triggers: tuple[str, ...] = ()) -> RunSummary | None:
        sql = "SELECT * FROM runs WHERE dry_run = 0"
        params: list[object] = []
        if triggers:
            sql += f" AND trigger IN ({','.join('?' * len(triggers))})"
            params.extend(triggers)
        with self._conn() as conn:
            row = conn.execute(sql + " ORDER BY started_at DESC LIMIT 1", params).fetchone()
        return self._summary(row) if row else None

    def get_report(self, run_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT report_json FROM runs WHERE id=?", (run_id,)).fetchone()
        return json.loads(row["report_json"]) if row else None

    @staticmethod
    def _summary(row: sqlite3.Row) -> RunSummary:
        return RunSummary(
            id=row["id"], trigger=row["trigger"], dry_run=bool(row["dry_run"]), started_at=row["started_at"],
            finished_at=row["finished_at"], status=row["status"], service_date=row["service_date"],
            sent=row["sent"], failed=row["failed"], skipped=row["skipped"], errors=row["errors"],
            warnings=row["warnings"],
        )

    # ------------------------------------------------------------------ chats seen via webhook

    def remember_chat(self, chat_id: str, kind: str, name: str = "", status: str = "active") -> None:
        now = _now()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO chats (chat_id, kind, name, status, first_seen, last_seen) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET name=CASE WHEN excluded.name != '' THEN excluded.name "
                "ELSE chats.name END, status=excluded.status, last_seen=excluded.last_seen",
                (chat_id, kind, name, status, now, now),
            )

    def chats(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM chats ORDER BY last_seen DESC").fetchall()
        return [dict(r) for r in rows]

    def chat(self, chat_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
        return dict(row) if row else None

    def forget_chat(self, chat_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM chats WHERE chat_id=?", (chat_id,))

    def set_member_count(self, chat_id: str, count: int) -> None:
        now = _now()
        kind = {"C": "group", "R": "room"}.get(chat_id[:1], "user")
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO chats (chat_id, kind, first_seen, last_seen, member_count, count_checked_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET member_count=excluded.member_count, "
                "count_checked_at=excluded.count_checked_at",
                (chat_id, kind, now, now, count, now),
            )

    def member_counts(self) -> dict[str, tuple[int, str]]:
        """群組 ID → (人數, 查詢時間)。只包含查過人數的群組。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT chat_id, member_count, count_checked_at FROM chats WHERE member_count IS NOT NULL"
            ).fetchall()
        return {r["chat_id"]: (r["member_count"], r["count_checked_at"]) for r in rows}

    # ------------------------------------------------------------------ people seen via webhook

    def person(self, user_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM people WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def remember_person(self, user_id: str, display_name: str = "", chat_id: str = "",
                        profile_checked: bool = False) -> None:
        """記下一個人出現過。``profile_checked`` = 這次有向 LINE 查過顯示名稱（查不到也算，避免一直重查）。"""
        now = _now()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO people (user_id, display_name, chat_id, first_seen, last_seen, profile_checked_at) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET display_name=CASE WHEN excluded.display_name != '' "
                "THEN excluded.display_name ELSE people.display_name END, chat_id=excluded.chat_id, "
                "last_seen=excluded.last_seen, profile_checked_at=CASE WHEN excluded.profile_checked_at != '' "
                "THEN excluded.profile_checked_at ELSE people.profile_checked_at END",
                (user_id, display_name, chat_id, now, now, now if profile_checked else ""),
            )
            if chat_id[:1] in ("C", "R"):
                conn.execute(
                    "INSERT INTO memberships (user_id, chat_id, first_seen, last_seen) VALUES (?,?,?,?) "
                    "ON CONFLICT(user_id, chat_id) DO UPDATE SET last_seen=excluded.last_seen",
                    (user_id, chat_id, now, now),
                )

    def claim_real_name(self, user_id: str, real_name: str) -> None:
        """本人登記的名字：後登記的蓋掉先登記的；重新登記代表他想被處理，所以取消「忽略」。"""
        now = _now()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO people (user_id, first_seen, last_seen, real_name, real_name_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET real_name=excluded.real_name, "
                "real_name_at=excluded.real_name_at, ignored=0",
                (user_id, now, now, real_name, now),
            )

    def clear_real_name(self, user_id: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE people SET real_name='', real_name_at='' WHERE user_id=?", (user_id,))

    def claim_nickname(self, user_id: str, nickname: str) -> str:
        """本人用「/我的暱稱」登記的稱呼：一個人可以有好幾個，不會蓋掉真實姓名。

        回傳 ""（已加入）、"duplicate"（早就登記過了）或 "full"（超過 MAX_NICKNAMES）。
        """
        now = _now()
        with self._conn() as conn:
            row = conn.execute("SELECT nicknames FROM people WHERE user_id=?", (user_id,)).fetchone()
            current = nicknames_of(dict(row) if row else None)
            if any(n.casefold() == nickname.casefold() for n in current):
                return "duplicate"
            if len(current) >= MAX_NICKNAMES:
                return "full"
            payload = json.dumps([*current, nickname], ensure_ascii=False)
            conn.execute(
                "INSERT INTO people (user_id, first_seen, last_seen, nicknames) VALUES (?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET nicknames=excluded.nicknames, ignored=0",
                (user_id, now, now, payload),
            )
        return ""

    def drop_nickname(self, user_id: str, nickname: str = "") -> None:
        """刪掉一個登記的暱稱（管理員處理過了）；``nickname`` 留空 = 全部清掉。"""
        with self._conn() as conn:
            row = conn.execute("SELECT nicknames FROM people WHERE user_id=?", (user_id,)).fetchone()
            if row is None:
                return
            keep = [n for n in nicknames_of(dict(row)) if nickname and n != nickname]
            conn.execute("UPDATE people SET nicknames=? WHERE user_id=?",
                         (json.dumps(keep, ensure_ascii=False) if keep else "", user_id))

    def set_person_ignored(self, user_id: str, ignored: bool) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE people SET ignored=? WHERE user_id=?", (int(ignored), user_id))

    def people(self) -> list[dict]:
        """所有收集到的人，附上「最後在哪個群組看到」和「在哪些群組」（群組名稱用「、」串起來）。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT people.*, last_chat.name AS chat_name, "
                "(SELECT group_concat(CASE WHEN c.name != '' THEN c.name ELSE m.chat_id END, '、') "
                " FROM memberships m LEFT JOIN chats c ON c.chat_id = m.chat_id "
                " WHERE m.user_id = people.user_id) AS chat_names "
                "FROM people LEFT JOIN chats AS last_chat ON last_chat.chat_id = people.chat_id "
                "ORDER BY people.last_seen DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def hand_over_chat(self, chat_id: str, other: "History") -> int:
        """把一個群組交給另一份資料庫（還沒分配的群組分到某個牧區時用）。

        群組本身、在裡面講過話的人、他們登記的名字都跟著過去，這裡不再留（還在別的群組出現的人除外）；
        對方已經認識的人以對方的為準，不覆蓋那邊已經確認過的資料。回傳交出去幾個人。
        """
        with self._conn() as conn:
            chat = conn.execute("SELECT * FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
            members = conn.execute("SELECT * FROM memberships WHERE chat_id=?", (chat_id,)).fetchall()
            people = conn.execute("SELECT p.* FROM people p JOIN memberships m ON m.user_id = p.user_id "
                                  "WHERE m.chat_id=?", (chat_id,)).fetchall()
        with other._conn() as conn:
            for table, rows in (("chats", [chat] if chat else []), ("memberships", members), ("people", people)):
                for row in rows:
                    cols = list(row.keys())  # 欄位名稱來自資料庫本身，不是使用者輸入
                    conn.execute(f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) "  # noqa: S608
                                 f"VALUES ({','.join('?' * len(cols))})", tuple(row))
        with self._conn() as conn:
            conn.execute("DELETE FROM memberships WHERE chat_id=?", (chat_id,))
            conn.execute("DELETE FROM chats WHERE chat_id=?", (chat_id,))
            for person in people:
                conn.execute("DELETE FROM people WHERE user_id=? AND user_id NOT IN (SELECT user_id FROM memberships)",
                             (person["user_id"],))
        return len(people)

    def forget_person(self, user_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM people WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM memberships WHERE user_id=?", (user_id,))
