"""把「一張表格的格子」轉成 Roster。

支援三種最常見的服事表排法（layout=auto 時自動判斷）：

wide：日期在左邊，一列一次聚會        long：一列一項服事             matrix：日期在上面，一欄一次聚會
  日期  | 講員   | 司琴 | 音控            日期 | 服事項目 | 人員           服事 | 9/7    | 9/14
  9/7   | 王牧師 | 小明 | 阿德            9/7  | 司琴     | 小明           講員 | 王牧師 | 李傳道
  9/14  | 李傳道 | 小華 | 阿德            9/7  | 音控     | 阿德           司琴 | 小明   | 小華

容錯：
* 表頭上面有標題列（例如「2026 第四季服事表」）沒關係，會往下找表頭。
* 合併儲存格（日期只寫在第一列）會自動沿用上一個日期。
* 一格多人：用 、 , / ; + & 換行 分開都可以；「小明 小華」這種中文名字用空白隔開也行。
* 括號裡的字不會被拆開：「小明(主領、吉他)」還是一個人。
* 「-」「無」「休」這種代表沒有人。
* 日期看不懂的列不會讓整份表壞掉，只會回報「第幾列日期看不懂」。
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Iterable

from church_bot.config import SourceSettings
from church_bot.core.dates import parse_date
from church_bot.errors import SourceError
from church_bot.models import Assignment, Issue, RawSheet, Roster, ServiceDay, Severity

HEADER_SCAN_ROWS = 15
MAX_ROW_ISSUES = 5
LAYOUT_ZH = {"wide": "日期在左、一列一次聚會", "long": "一列一項服事", "matrix": "日期在上、一欄一次聚會"}

_SPACE_RE = re.compile(r"\s+")
_CJK_NAME_RE = re.compile(r"^[㐀-鿿豈-﫿·・‧]+$")
_SEPARATORS = set("、,，;；/／&＆+＋\n\r")
_OPEN, _CLOSE = set("(（[【「"), set(")）]】」")
# 排班用的輔助欄位（不是服事項目）：表頭「完全等於」這些字就不會出現在提醒裡
_BOOKKEEPING_HEADERS = {"月份", "月", "週次", "週別", "週", "第幾週", "星期", "禮拜", "month", "week", "weekday", "no", "#"}


def clean_header(text: str) -> str:
    return _SPACE_RE.sub(" ", text or "").strip()


def _norm(text: str) -> str:
    return _SPACE_RE.sub("", text or "").lower()


def _split_outside_brackets(text: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in text:
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth = max(0, depth - 1)
        if depth == 0 and ch in _SEPARATORS:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def split_names(cell: str, empty_markers: Iterable[str] = ()) -> tuple[str, ...]:
    markers = {m.strip().lower() for m in empty_markers if m.strip()}
    text = (cell or "").strip()
    if not text or text.lower() in markers:
        return ()
    names: list[str] = []
    for chunk in _split_outside_brackets(text):
        tokens = chunk.split()
        if not tokens:
            continue
        # 「小明 小華」→ 兩個人；「John Chen」、「小明 (代)」→ 一個人
        if len(tokens) > 1 and all(_CJK_NAME_RE.match(t) for t in tokens):
            names.extend(tokens)
        else:
            names.append(" ".join(tokens))
    return tuple(dict.fromkeys(n for n in names if n.lower() not in markers))


def _cell(row: list[str], idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return (row[idx] or "").strip()


@dataclass(slots=True)
class _Layout:
    kind: str
    header_row: int
    cols: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class _DayDraft:
    roles: dict[str, list[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


class _DayBuilder:
    """依 (日期, 聚會) 收集服事；同一天同一項服事出現多次會合併名字。"""

    def __init__(self) -> None:
        self._days: dict[tuple[dt.date, str], _DayDraft] = {}

    def add(self, date: dt.date, label: str, role: str, names: tuple[str, ...]) -> None:
        draft = self._days.setdefault((date, label), _DayDraft())
        bucket = draft.roles.setdefault(role, [])
        bucket.extend(n for n in names if n not in bucket)

    def note(self, date: dt.date, label: str, note: str) -> None:
        if note:
            draft = self._days.setdefault((date, label), _DayDraft())
            if note not in draft.notes:
                draft.notes.append(note)

    def build(self) -> tuple[ServiceDay, ...]:
        order = {key: i for i, key in enumerate(self._days)}
        days = [
            ServiceDay(
                date=date,
                label=label,
                assignments=tuple(Assignment(role, tuple(names)) for role, names in draft.roles.items()),
                note="；".join(draft.notes),
            )
            for (date, label), draft in self._days.items()
        ]
        days.sort(key=lambda d: (d.date, order[(d.date, d.label)]))
        return tuple(days)


class RosterParser:
    def __init__(self, cfg: SourceSettings, today: dt.date) -> None:
        self.cfg = cfg
        self.today = today
        self._row_issues: list[str] = []

    # ------------------------------------------------------------------ public

    def parse(self, sheet: RawSheet) -> Roster:
        self._row_issues = []
        rows = [[(c if isinstance(c, str) else str(c or "")) for c in row] for row in sheet.rows]
        if not any(any(c.strip() for c in row) for row in rows):
            raise SourceError(
                "服事表是空的（一格資料都沒有）",
                "確認分頁名稱有沒有選對；如果是 Google Sheet，確認連結是正確的那一份。",
            )
        layout = self._detect(rows)
        days = {"wide": self._parse_wide, "long": self._parse_long, "matrix": self._parse_matrix}[layout.kind](
            rows, layout
        )

        issues = [Issue(Severity.INFO, "layout", f"服事表格式判斷為：{LAYOUT_ZH[layout.kind]}（{layout.kind}）"
                        f"，表頭在第 {layout.header_row + 1} 列")]
        for msg in self._row_issues[:MAX_ROW_ISSUES]:
            issues.append(Issue(Severity.WARNING, "row_unreadable", msg, "請檢查服事表那一列的日期寫法，例如 2026/9/13。"))
        if len(self._row_issues) > MAX_ROW_ISSUES:
            issues.append(Issue(Severity.WARNING, "row_unreadable_more",
                                f"另外還有 {len(self._row_issues) - MAX_ROW_ISSUES} 列也看不懂"))
        if not days:
            issues.append(Issue(Severity.ERROR, "roster_no_dates", "服事表裡找不到任何看得懂的日期",
                                "日期請寫成 2026/9/13 或 9/13 這種格式。"))
        return Roster(days=days, source=sheet.source, issues=tuple(issues))

    # ------------------------------------------------------------------ detection

    def _match(self, header: str, key: str) -> bool:
        h = _norm(header)
        return bool(h) and any(_norm(a) and _norm(a) in h for a in getattr(self.cfg.columns, key))

    def _is_ignored(self, header: str) -> bool:
        return _norm(header) in _BOOKKEEPING_HEADERS or self._match(header, "ignore")

    def _text(self, row: list[str], idx: int | None) -> str:
        """備註、聚會名稱這種「一段文字」的格子；寫「-」「無」之類的就當作空白。"""
        text = _cell(row, idx)
        return "" if text.lower() in {m.strip().lower() for m in self.cfg.empty_markers} else text

    def _map_columns(self, cells: list[str]) -> dict[str, int]:
        cols: dict[str, int] = {}
        for idx, cell in enumerate(cells):
            for key in ("date", "person", "role", "label", "note"):
                if key not in cols and self._match(cell, key):
                    cols[key] = idx
                    break
        return cols

    def _is_date(self, text: str) -> bool:
        return bool(text) and parse_date(text, self.today) is not None

    def _detect(self, rows: list[list[str]]) -> _Layout:
        forced = self.cfg.layout
        for i, row in enumerate(rows[:HEADER_SCAN_ROWS]):
            cells = [c.strip() for c in row]
            rest = [c for c in cells[1:] if c]
            dated = sum(1 for c in rest if self._is_date(c))
            if forced in ("auto", "matrix") and dated >= 1 and dated >= len(rest) * 0.6 and (
                dated >= 2 or forced == "matrix"
            ):
                return _Layout("matrix", i)

            cols = self._map_columns(cells)
            if "date" not in cols:
                continue
            is_long = "role" in cols and "person" in cols
            if forced == "long" or (forced == "auto" and is_long):
                if not is_long:
                    raise SourceError(
                        "設定成「一列一項服事（long）」格式，但表頭找不到「服事項目」和「人員」欄位",
                        "把表頭改成「日期、服事項目、人員」，或把設定裡的格式改回「自動判斷」。",
                    )
                return _Layout("long", i, cols)
            if forced in ("auto", "wide"):
                return _Layout("wide", i, cols)

        # 沒有「日期」表頭，但第一欄大部分是日期 → 當作 wide，表頭是第一個日期的上一列
        first_dated = next((i for i, r in enumerate(rows[:HEADER_SCAN_ROWS + 1]) if self._is_date(_cell(r, 0))), None)
        if forced in ("auto", "wide") and first_dated:
            return _Layout("wide", first_dated - 1, {"date": 0})

        raise SourceError(
            "服事表裡找不到「日期」欄位",
            "表頭（第一列）請放一格「日期」；或把日期寫在最左邊那一欄。格式可以參考 docs/SHEET_FORMAT.md。",
        )

    def _bad_row(self, line_no: int, text: str) -> None:
        self._row_issues.append(f"服事表第 {line_no} 列的日期看不懂：「{text}」，這一列先跳過")

    # ------------------------------------------------------------------ layouts

    def _parse_wide(self, rows: list[list[str]], layout: _Layout) -> tuple[ServiceDay, ...]:
        header = [clean_header(c) for c in rows[layout.header_row]]
        date_col, label_col, note_col = layout.cols["date"], layout.cols.get("label"), layout.cols.get("note")
        special = {date_col, label_col, note_col}
        role_cols = [(i, h) for i, h in enumerate(header) if i not in special and h and not self._is_ignored(h)]

        builder = _DayBuilder()
        last_date: dt.date | None = None
        last_label = ""
        for r_idx in range(layout.header_row + 1, len(rows)):
            row = rows[r_idx]
            date_text = _cell(row, date_col)
            has_content = any(_cell(row, i) for i, _ in role_cols)
            if date_text:
                date = parse_date(date_text, self.today)
                if date is None:
                    if has_content:
                        self._bad_row(r_idx + 1, date_text)
                    continue  # 沒內容的看不懂列（例如「十月」分隔列）直接跳過
                label = self._text(row, label_col)
            elif has_content and last_date:
                date, label = last_date, self._text(row, label_col) or last_label  # 合併儲存格
            else:
                continue
            last_date, last_label = date, label
            for i, role in role_cols:
                builder.add(date, label, role, split_names(_cell(row, i), self.cfg.empty_markers))
            builder.note(date, label, self._text(row, note_col))
        return builder.build()

    def _parse_long(self, rows: list[list[str]], layout: _Layout) -> tuple[ServiceDay, ...]:
        c = layout.cols
        builder = _DayBuilder()
        last_date: dt.date | None = None
        last_role = last_label = ""
        for r_idx in range(layout.header_row + 1, len(rows)):
            row = rows[r_idx]
            date_text = _cell(row, c["date"])
            role_text = clean_header(_cell(row, c["role"]))
            person = _cell(row, c["person"])
            if not (date_text or role_text or person):
                continue
            if date_text:
                date = parse_date(date_text, self.today)
                if date is None:
                    self._bad_row(r_idx + 1, date_text)
                    continue
                if date != last_date:
                    last_role = ""
                label = _cell(row, c.get("label"))
            elif last_date:
                date, label = last_date, _cell(row, c.get("label")) or last_label
            else:
                continue
            last_date, last_label = date, label
            role = role_text or last_role
            last_role = role
            builder.note(date, label, self._text(row, c.get("note")))
            if role and not self._is_ignored(role):
                builder.add(date, label, role, split_names(person, self.cfg.empty_markers))
        return builder.build()

    def _parse_matrix(self, rows: list[list[str]], layout: _Layout) -> tuple[ServiceDay, ...]:
        header = rows[layout.header_row]
        date_cols = [(j, d) for j in range(1, len(header)) if (d := parse_date(_cell(header, j), self.today))]
        labels: dict[int, str] = {}
        notes: dict[int, str] = {}
        cells: list[tuple[str, int, tuple[str, ...]]] = []
        last_role = ""
        for r_idx in range(layout.header_row + 1, len(rows)):
            row = rows[r_idx]
            role = clean_header(_cell(row, 0))
            if not role:
                if not (last_role and any(_cell(row, j) for j, _ in date_cols)):
                    continue
                role = last_role  # 同一項服事分兩列寫
            last_role = role
            if self._is_ignored(role) or self._match(role, "date"):
                continue
            if self._match(role, "label"):
                labels.update({j: _cell(row, j) for j, _ in date_cols if _cell(row, j)})
                continue
            if self._match(role, "note"):
                notes.update({j: self._text(row, j) for j, _ in date_cols if self._text(row, j)})
                continue
            for j, _ in date_cols:
                cells.append((role, j, split_names(_cell(row, j), self.cfg.empty_markers)))

        builder = _DayBuilder()
        for j, date in date_cols:
            builder.note(date, labels.get(j, ""), notes.get(j, ""))
        for role, j, names in cells:
            builder.add(dict(date_cols)[j], labels.get(j, ""), role, names)
        return builder.build()


def parse_roster(sheet: RawSheet, cfg: SourceSettings, today: dt.date) -> Roster:
    return RosterParser(cfg, today).parse(sheet)
