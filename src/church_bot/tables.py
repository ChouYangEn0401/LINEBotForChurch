"""對照表（CSV）讀寫：``config/targets.csv``（群組表）與 ``config/members.csv``（人員表）。

為什麼用 CSV：
* 用 Excel / Numbers / Google Sheet / 記事本都能開，看得見、改得動。
* 網頁介面也能直接改，兩邊改的是同一個檔案。

為了讓不熟電腦的人也不容易弄壞：
* 表頭可以用中文或英文、前後有空白也沒關係。
* 用 Excel 另存成 Big5 也讀得懂；存檔一律寫 UTF-8 with BOM，Excel 打開不會亂碼。
* 是/否欄位接受：是、否、Y、N、V、✓、1、0、TRUE、FALSE、空白。
* 有問題不會直接爆掉，而是回傳 Issue 讓畫面顯示「第幾列怎麼了」。
"""

from __future__ import annotations

import csv
import io
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Generic, Iterable, TypeVar

from church_bot.errors import TableError
from church_bot.models import Issue, Member, Severity, Target

LINE_ID_RE = re.compile(r"^[CUR][0-9a-f]{32}$")
# 網頁、Webhook 可能同時「讀 → 改 → 存」同一張表；同一個程式裡用這把鎖排隊
TABLE_WRITE_LOCK = threading.RLock()
LIST_SPLIT_RE = re.compile(r"[、,，;；/／\n]+")

TRUE_WORDS = {"是", "y", "yes", "v", "✓", "✔", "1", "true", "t", "o", "on", "啟用", "要"}
FALSE_WORDS = {"否", "n", "no", "x", "✗", "0", "false", "f", "off", "停用", "不要"}


def parse_bool(value: str, default: bool) -> bool | None:
    """回傳 None 代表看不懂。"""
    v = (value or "").strip().lower()
    if not v:
        return default
    if v in TRUE_WORDS:
        return True
    if v in FALSE_WORDS:
        return False
    return None


def parse_list(value: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in LIST_SPLIT_RE.split(value or "") if p.strip())


def fmt_bool(value: bool) -> str:
    return "是" if value else "否"


def fmt_list(values: Iterable[str]) -> str:
    return "、".join(values)


def describe_line_id(line_id: str) -> str:
    return {"C": "群組", "U": "個人", "R": "多人聊天室"}.get(line_id[:1], "未知")


# --------------------------------------------------------------------------- CSV I/O


def read_csv_text(path: Path) -> tuple[str, str]:
    """讀 CSV 並自動判斷編碼。回傳 (內容, 使用的編碼)。"""
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp950", "big5hkscs"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    raise TableError(
        f"{path.name} 的文字編碼看不懂",
        "請用 Excel 開啟後「另存新檔」→ 選「CSV UTF-8（逗號分隔）」。",
    )


def write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    tmp = path.with_name(path.name + ".tmp")
    # utf-8-sig：Windows 的 Excel 直接雙擊打開中文才不會亂碼
    tmp.write_text(buf.getvalue(), encoding="utf-8-sig")
    tmp.replace(path)


# --------------------------------------------------------------------------- generic table

T = TypeVar("T")


def _norm_header(text: str) -> str:
    return re.sub(r"[\s_]+", "", text).lower()


@dataclass(frozen=True, slots=True)
class Column:
    key: str
    header: str  # 存檔時用的表頭（中文）
    aliases: tuple[str, ...] = ()
    required: bool = False

    def matches(self, header: str) -> bool:
        return _norm_header(header) in {_norm_header(a) for a in (self.header, self.key, *self.aliases)}


@dataclass(slots=True)
class TableResult(Generic[T]):
    items: list[T]
    issues: list[Issue]


class CsvTable(Generic[T]):
    """一張 CSV 對照表的讀寫邏輯；具體欄位與轉換由子類別決定。"""

    title: str = ""
    columns: tuple[Column, ...] = ()

    def __init__(self, path: Path) -> None:
        self.path = path

    # --- 子類別實作 ---
    def from_row(self, row: dict[str, str], line_no: int, issues: list[Issue]) -> T | None:
        raise NotImplementedError

    def to_row(self, item: T) -> list[str]:
        raise NotImplementedError

    def validate_all(self, items: list[T], issues: list[Issue]) -> None:
        """跨列檢查（例如重複）。"""

    # --- 共用 ---
    def _issue(self, severity: Severity, code: str, message: str, hint: str = "") -> Issue:
        return Issue(severity, code, f"【{self.title}】{message}", hint)

    def load(self) -> TableResult[T]:
        if not self.path.exists():
            return TableResult([], [self._issue(
                Severity.WARNING, "table_missing", f"還沒有 {self.path.name}",
                "到網頁上新增第一筆資料就會自動建立。",
            )])
        text, encoding = read_csv_text(self.path)
        issues: list[Issue] = []
        rows = list(csv.reader(io.StringIO(text)))
        if not rows:
            return TableResult([], issues)

        mapping: dict[int, str] = {}
        for idx, header in enumerate(h.strip() for h in rows[0]):
            for col in self.columns:
                if col.key not in mapping.values() and col.matches(header):
                    mapping[idx] = col.key
                    break
        missing = [c.header for c in self.columns if c.required and c.key not in mapping.values()]
        if missing:
            raise TableError(
                f"{self.path.name} 第一列（表頭）缺少：{'、'.join(missing)}",
                f"第一列必須是：{'、'.join(c.header for c in self.columns)}。可參考 {self.path.stem}.example.csv。",
            )

        items: list[T] = []
        for line_no, raw in enumerate(rows[1:], start=2):
            if not any(cell.strip() for cell in raw):
                continue  # 空白列直接跳過
            row = {key: (raw[idx].strip() if idx < len(raw) else "") for idx, key in mapping.items()}
            item = self.from_row(row, line_no, issues)
            if item is not None:
                items.append(item)
        self.validate_all(items, issues)

        if encoding != "utf-8-sig":
            # 用 Excel 存成 Big5 的情況：讀得懂，順手轉回 UTF-8，之後就不會再有問題
            self.save(items)
            issues.append(self._issue(
                Severity.INFO, "table_reencoded", f"{self.path.name} 原本是 {encoding} 編碼，已自動轉成 UTF-8"
            ))
        return TableResult(items, issues)

    def save(self, items: list[T]) -> None:
        write_csv(self.path, [c.header for c in self.columns], [self.to_row(i) for i in items])


# --------------------------------------------------------------------------- targets.csv


class TargetTable(CsvTable[Target]):
    title = "群組表"
    columns = (
        Column("name", "群組名稱", ("名稱", "群組", "name"), required=True),
        Column("line_id", "LINE_ID", ("LINE ID", "ID", "群組ID", "line_id", "to"), required=True),
        Column("enabled", "啟用", ("enabled", "開啟")),
        Column("roles", "只發這些服事", ("服事項目", "roles", "只發服事")),
        Column("labels", "只發這些聚會", ("聚會", "labels", "只發聚會")),
        Column("mention", "標記人員", ("@人", "mention", "tag")),
        Column("note", "備註", ("note", "說明")),
    )

    def from_row(self, row: dict[str, str], line_no: int, issues: list[Issue]) -> Target | None:
        name = row.get("name", "")
        line_id = row.get("line_id", "").strip()
        where = f"第 {line_no} 列（{name or '沒有名稱'}）"
        if not name:
            issues.append(self._issue(Severity.WARNING, "target_no_name", f"{where}沒有填群組名稱，已略過"))
            return None

        enabled = parse_bool(row.get("enabled", ""), default=True)
        if enabled is None:
            issues.append(self._issue(
                Severity.WARNING, "target_bad_bool", f"{where}「啟用」看不懂：{row.get('enabled')}",
                "請填「是」或「否」。這次先當作「否」。",
            ))
            enabled = False
        mention = parse_bool(row.get("mention", ""), default=False) or False

        if enabled and not line_id:
            issues.append(self._issue(
                Severity.ERROR, "target_no_id", f"{where}還沒填 LINE_ID，不會收到提醒",
                "把機器人加進群組後，在群組裡打「/群組ID」就會自動抓到；或先把「啟用」改成「否」。",
            ))
        elif line_id and not LINE_ID_RE.match(line_id):
            issues.append(self._issue(
                Severity.ERROR, "target_bad_id", f"{where}的 LINE_ID 格式不對：{line_id}",
                "正確格式是 C 或 U 開頭再加 32 個英數字（共 33 碼）。注意不要多複製到空白。",
            ))
        return Target(
            name=name, line_id=line_id, enabled=enabled,
            roles=parse_list(row.get("roles", "")), labels=parse_list(row.get("labels", "")),
            mention=mention, note=row.get("note", ""),
        )

    def to_row(self, t: Target) -> list[str]:
        return [t.name, t.line_id, fmt_bool(t.enabled), fmt_list(t.roles), fmt_list(t.labels),
                fmt_bool(t.mention), t.note]

    def validate_all(self, items: list[Target], issues: list[Issue]) -> None:
        seen: dict[str, str] = {}
        for t in items:
            if t.line_id and t.enabled and t.line_id in seen:
                issues.append(self._issue(
                    Severity.WARNING, "target_dup_id",
                    f"「{t.name}」和「{seen[t.line_id]}」是同一個 LINE_ID，會收到兩次提醒",
                ))
            if t.line_id and t.enabled:
                seen.setdefault(t.line_id, t.name)
        if not any(t.enabled and LINE_ID_RE.match(t.line_id) for t in items):
            issues.append(self._issue(
                Severity.ERROR, "no_active_target", "目前沒有任何可以收到提醒的群組",
                "提醒不會送到任何地方。請到「群組」頁新增群組並填好 LINE_ID。",
            ))


# --------------------------------------------------------------------------- members.csv


class MemberTable(CsvTable[Member]):
    title = "人員表"
    columns = (
        Column("name", "名字", ("姓名", "name", "顯示名稱"), required=True),
        Column("aliases", "其他寫法", ("別名", "綽號", "aliases", "alias")),
        Column("line_user_id", "LINE_userId", ("userId", "line_user_id")),
        Column("active", "啟用", ("active", "在職", "服事中")),
        Column("note", "備註", ("note", "說明")),
    )

    def from_row(self, row: dict[str, str], line_no: int, issues: list[Issue]) -> Member | None:
        name = row.get("name", "")
        if not name:
            issues.append(self._issue(Severity.WARNING, "member_no_name", f"第 {line_no} 列沒有填名字，已略過"))
            return None
        uid = row.get("line_user_id", "").strip()
        if uid and not (LINE_ID_RE.match(uid) and uid.startswith("U")):
            issues.append(self._issue(
                Severity.WARNING, "member_bad_uid", f"「{name}」的 LINE_userId 格式不對：{uid}",
                "userId 是 U 開頭再加 32 碼。不確定就留白，只是不能 @ 他而已。",
            ))
            uid = ""
        active = parse_bool(row.get("active", ""), default=True)
        return Member(
            name=name, aliases=parse_list(row.get("aliases", "")), line_user_id=uid,
            active=True if active is None else active, note=row.get("note", ""),
        )

    def to_row(self, m: Member) -> list[str]:
        return [m.name, fmt_list(m.aliases), m.line_user_id, fmt_bool(m.active), m.note]

    def validate_all(self, items: list[Member], issues: list[Issue]) -> None:
        owner: dict[str, str] = {}
        for m in items:
            for key in {m.name, *m.aliases}:
                k = key.strip()
                if k in owner and owner[k] != m.name:
                    issues.append(self._issue(
                        Severity.WARNING, "member_dup_name", f"「{k}」同時出現在「{owner[k]}」和「{m.name}」",
                        "服事表寫這個名字時只會對到第一位，請把其中一個改掉。",
                    ))
                owner.setdefault(k, m.name)


def upsert(items: list[T], new: T, key: Callable[[T], str], original_key: str | None = None) -> list[T]:
    """新增或取代一筆資料。

    ``original_key`` 是編輯前的 key（改名時用）；找不到就加在最後。
    改名撞到另一筆既有資料時，以新的為準（舊的那筆移除），避免出現兩筆同名。
    """
    match = original_key if original_key else key(new)
    out: list[T] = []
    replaced = False
    for item in items:
        k = key(item)
        if k == match and not replaced:
            out.append(new)
            replaced = True
        elif k == key(new):
            continue
        else:
            out.append(item)
    if not replaced:
        out.append(new)
    return out


def remove(items: list[T], target_key: str, key: Callable[[T], str]) -> list[T]:
    return [i for i in items if key(i) != target_key]
