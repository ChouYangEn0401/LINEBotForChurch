"""人員調換（覆蓋層）：服事表還是 Google Sheet 為準，調換疊在上面。

一個牧區一份 ``data/ministries/<id>/overrides.json``。每一筆是一次調換：

* ``person``：某一天某一項，把 A 換成 B（「9/13 司琴：小明 → 美華」）。
* ``weeks``：兩天對調。整天（assignments＋照原樣顯示的欄位，日期和聚會名不動）、只換某一項（``role``）、
  或只換某一欄（``column``，例如服飾）。

為什麼不寫回 Google Sheet：兩種讀法（公開連結、服務帳號檢視者）都是唯讀；要寫回得換成編輯權限，
改錯就是改到正本。覆蓋層改錯了 ``swap undo`` 就回來，Sheet 一個字都沒動。

套用的地方只有一個：``BotService.fetch_roster``（parser 之後、所有用到服事表的地方之前），
所以每週推播、``/提醒``、``/行事曆``、管理網頁的預覽、指令列的 ``preview``／``send``／``roster`` 看到的都是調換後的。

每一筆套用時看三種情況（照建立的順序一筆一筆疊）：

1. **服事表已經是調換後的樣子**（有人把 Sheet 改好了）→ 那一筆標 ``retired``、寫回檔案，之後不再套。
2. **服事表還是建立調換時的樣子** → 套用，那一天標「已調整」。
3. **都不是**（Sheet 那一格又被改成別的了）→ 這次不套（以 Sheet 為準），那一筆照樣是 ``active``，
   ``swap list`` 會寫為什麼沒套上。不自動退掉：可能只是暫時改的，讓人決定要 undo 還是留著。

寫入一律走 ``files.write_text``（原子寫入＋變更紀錄，見 core/versions.py），讀改寫整段用跨程式的鎖排隊
（管理網頁、指令列、自動退掉可能同時寫）。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import secrets
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Iterable, Iterator

from church_bot.core.dates import WEEKDAY_CHARS
from church_bot.core.directory import Directory, normalize_name
from church_bot.errors import ChurchBotError
from church_bot.files import read_text, write_text
from church_bot.locking import process_lock
from church_bot.models import Assignment, Issue, Roster, ServiceDay, Severity

log = logging.getLogger(__name__)

FILE_NAME = "overrides.json"
ACTIVE, RETIRED, UNDONE = "active", "retired", "undone"
STATUS_ZH = {ACTIVE: "生效中", RETIRED: "已自動退掉（服事表已經改好）", UNDONE: "已退回"}
KINDS = ("person", "weeks")
LOCK_TIMEOUT = 30
_LOCK = threading.Lock()  # 同一個程式裡的讀改寫排隊；跨程式靠 process_lock


class SwapError(ChurchBotError):
    """調換沒做成（找不到那一天、那一格不是這個人…）。指令列的結束碼是 1，``extra`` 會放進 JSON。"""

    def __init__(self, message: str, hint: str = "", **extra: object) -> None:
        super().__init__(message, hint)
        self.extra = extra


# --------------------------------------------------------------------------- 檔案


def file_of(paths) -> Path:  # noqa: ANN001 - config.Paths（避免循環匯入）
    return paths.data_dir / FILE_NAME


def _read(path: Path) -> list[dict]:
    """讀檔；壞掉丟 ValueError。"""
    data = json.loads(read_text(path))
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError("items 不是清單")
    return [i for i in items if isinstance(i, dict)]


def load(paths) -> list[dict]:  # noqa: ANN001
    """全部的調換（含已退回、已退掉的）。檔案壞了不丟例外：套用時當作沒有調換，提醒照常發。"""
    path = file_of(paths)
    if not path.exists():
        return []
    try:
        return _read(path)
    except (ValueError, OSError, ChurchBotError) as exc:
        log.error("人員調換的檔案 %s 讀不懂，這次先不套用任何調換：%s", path, exc)
        return []


def _load_for_write(paths) -> list[dict]:  # noqa: ANN001
    path = file_of(paths)
    if not path.exists():
        return []
    try:
        return _read(path)
    except (ValueError, OSError) as exc:
        raise ChurchBotError(f"人員調換的檔案（{path.name}）讀不懂，先不寫入，免得蓋掉裡面的資料",
                             "到管理網頁「變更紀錄」把它還原成上一版，或請維護的人看一下這個檔案。") from exc


def _save(paths, items: list[dict]) -> None:  # noqa: ANN001
    text = json.dumps({"version": 1, "items": items}, ensure_ascii=False, indent=2) + "\n"
    write_text(file_of(paths), text)


@contextmanager
def _locked(paths) -> Iterator[None]:  # noqa: ANN001
    """讀改寫整段排隊：同一個程式用 threading.Lock，跨程式用 data/ministries/<id>/overrides.lock。"""
    with _LOCK, process_lock(paths.data_dir / "overrides.lock", timeout=LOCK_TIMEOUT):
        yield


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _new_id(items: list[dict]) -> str:
    used = {str(i.get("id")) for i in items}
    while True:
        candidate = secrets.token_hex(3)
        if candidate not in used:
            return candidate


# --------------------------------------------------------------------------- 給人看的字


def when(day: dt.date | str, label: str = "") -> str:
    d = dt.date.fromisoformat(day) if isinstance(day, str) else day
    text = f"{d.month}/{d.day}（{WEEKDAY_CHARS[d.weekday()]}）"
    return f"{text} {label}" if label else text


def summary(item: dict) -> str:
    """一句話：「9/13（日）主日崇拜 司琴：小明 → 美華」「9/13（日） ⇄ 9/20（日）：服飾」。"""
    try:
        if item.get("kind") == "person":
            return f"{when(item['date'], item.get('label', ''))} {item['role']}：{item['from']} → {item['to']}"
        what = (f"「{item['role']}」" if item.get("role") else
                f"「{item['column']}」" if item.get("column") else "整天")
        return f"{when(item['a'], item.get('label_a', ''))} ⇄ {when(item['b'], item.get('label_b', ''))}：{what}對調"
    except (KeyError, ValueError, TypeError):
        return f"（看不懂的一筆：{item.get('id', '?')}）"


def dates_of(item: dict) -> list[str]:
    return [item["date"]] if item.get("kind") == "person" else [item.get("a", ""), item.get("b", "")]


# --------------------------------------------------------------------------- 找那一天、那一格


def _days_on(days: Iterable[ServiceDay], date: dt.date) -> list[tuple[int, ServiceDay]]:
    return [(i, d) for i, d in enumerate(days) if d.date == date]


def _find_day(days: list[ServiceDay], date: dt.date, label: str) -> tuple[int, ServiceDay] | None:
    """套用時用：日期＋建立時記下的聚會名（同一天兩場聚會才分得開）。"""
    hits = _days_on(days, date)
    exact = [h for h in hits if h[1].label == label]
    if exact:
        return exact[0]
    return hits[0] if len(hits) == 1 and not label else None


def _pick_day(days: list[ServiceDay], date: dt.date, label: str, flag: str) -> tuple[int, ServiceDay]:
    """建立時用：找不到、或同一天有兩場又沒說哪一場，都講清楚。"""
    hits = _days_on(days, date)
    if not hits:
        nearby = sorted({d.date for d in days if abs((d.date - date).days) <= 14})
        raise SwapError(f"服事表上沒有 {when(date)} 這一天",
                        "看一下日期有沒有打錯（YYYY-MM-DD）；`roster show` 列得出服事表上有哪幾天。",
                        nearby=[d.isoformat() for d in nearby])
    if label:
        wanted = normalize_name(label)
        chosen = [h for h in hits if normalize_name(h[1].label) == wanted]
        if not chosen:
            raise SwapError(f"{when(date)} 沒有「{label}」這場聚會",
                            f"那一天的聚會：{'、'.join(h[1].label or '（沒寫名字）' for h in hits)}",
                            labels=[h[1].label for h in hits])
        return chosen[0]
    if len(hits) > 1:
        raise SwapError(f"{when(date)} 有 {len(hits)} 場聚會，要說是哪一場",
                        f"加上 {flag}=聚會名：{'、'.join(h[1].label or '（沒寫名字）' for h in hits)}",
                        labels=[h[1].label for h in hits])
    return hits[0]


def _role_of(day: ServiceDay, role: str) -> Assignment | None:
    role = (role or "").strip()
    exact = next((a for a in day.assignments if a.role == role), None)
    if exact is not None:
        return exact
    wanted = normalize_name(role)
    return next((a for a in day.assignments if normalize_name(a.role) == wanted), None)


def _column_of(days: Iterable[ServiceDay], column: str) -> str:
    """欄位的真正寫法（「服飾」也對得到表頭的「服飾」；大小寫、全半形不計）。"""
    wanted = normalize_name(column)
    for day in days:
        for key, _value in day.texts:
            if key == column or normalize_name(key) == wanted:
                return key
    return ""


def _name_key(name: str) -> str:
    return normalize_name(name)


def _match_name(names: Iterable[str], wanted: str, directory: Directory | None) -> str:
    """那一格裡哪一個名字是 ``wanted``：服事表上的寫法或訊息上的正式名字都算。回傳服事表上的寫法。"""
    key = _name_key(wanted)
    names = list(names)
    for raw in names:
        if _name_key(raw) == key:
            return raw
    if directory is not None:
        for raw in names:
            if _name_key(directory.resolve(raw).display) == key:
                return raw
        resolved = directory.resolve(wanted)
        if resolved.matched:
            for raw in names:
                if _name_key(directory.resolve(raw).display) == _name_key(resolved.display):
                    return raw
    return ""


# --------------------------------------------------------------------------- 內容快照（比對「是不是原本那樣」）


def _part(day: ServiceDay, role: str, column: str) -> object:
    if role:
        a = _role_of(day, role)
        return None if a is None else [_name_key(n) for n in a.names]
    if column:
        return day.text_of(column)
    return {"assignments": [[a.role, [_name_key(n) for n in a.names]] for a in day.assignments],
            "texts": [[k, v] for k, v in day.texts if v]}


def _mark(day: ServiceDay, *keys: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys([*day.adjusted, *keys]))


def _set_text(texts: tuple[tuple[str, str], ...], key: str, value: str) -> tuple[tuple[str, str], ...]:
    out = [(k, v) for k, v in texts if k != key]
    if value:
        out.append((key, value))
    order = [k for k, _ in texts]
    return tuple(sorted(out, key=lambda kv: order.index(kv[0]) if kv[0] in order else len(order)))


def _swap(a: ServiceDay, b: ServiceDay, role: str, column: str) -> tuple[ServiceDay, ServiceDay]:
    if role:
        ra, rb = _role_of(a, role), _role_of(b, role)
        assert ra is not None and rb is not None
        new_a = replace(a, assignments=tuple(replace(x, names=rb.names) if x is ra else x for x in a.assignments),
                        adjusted=_mark(a, ra.role))
        new_b = replace(b, assignments=tuple(replace(x, names=ra.names) if x is rb else x for x in b.assignments),
                        adjusted=_mark(b, rb.role))
        return new_a, new_b
    if column:
        va, vb = a.text_of(column), b.text_of(column)
        return (replace(a, texts=_set_text(a.texts, column, vb), adjusted=_mark(a, column)),
                replace(b, texts=_set_text(b.texts, column, va), adjusted=_mark(b, column)))
    return (replace(a, assignments=b.assignments, texts=b.texts, adjusted=_mark(a, "*")),
            replace(b, assignments=a.assignments, texts=a.texts, adjusted=_mark(b, "*")))


# --------------------------------------------------------------------------- 套用


def _apply_one(item: dict, days: list[ServiceDay]) -> str:
    """把一筆疊到 days 上（直接改 days）。回傳 "applied"、"retire" 或沒套上的原因（給人看的一句話）。"""
    kind = item.get("kind")
    if kind == "person":
        date = dt.date.fromisoformat(item["date"])
        found = _find_day(days, date, item.get("label", ""))
        if found is None:
            return f"服事表上已經沒有 {when(date, item.get('label', ''))}"
        idx, day = found
        a = _role_of(day, item["role"])
        if a is None:
            return f"{when(date)} 已經沒有「{item['role']}」這一項"
        keys = [_name_key(n) for n in a.names]
        has_from, has_to = _name_key(item["from"]) in keys, _name_key(item["to"]) in keys
        if has_to and not has_from:
            return "retire"
        if not has_from:
            return f"那一格現在是「{'、'.join(a.names) or '（空白）'}」，已經不是「{item['from']}」"
        names = tuple(item["to"] if _name_key(n) == _name_key(item["from"]) else n for n in a.names)
        days[idx] = replace(day, assignments=tuple(replace(x, names=names) if x is a else x for x in day.assignments),
                            adjusted=_mark(day, a.role))
        return "applied"
    if kind == "weeks":
        date_a, date_b = dt.date.fromisoformat(item["a"]), dt.date.fromisoformat(item["b"])
        fa = _find_day(days, date_a, item.get("label_a", ""))
        fb = _find_day(days, date_b, item.get("label_b", ""))
        if fa is None or fb is None:
            missing = date_a if fa is None else date_b
            return f"服事表上已經沒有 {when(missing)}"
        (ia, a), (ib, b) = fa, fb
        role, column = item.get("role", ""), item.get("column", "")
        if role and (_role_of(a, role) is None or _role_of(b, role) is None):
            return f"有一天已經沒有「{role}」這一項"
        before = item.get("before") or {}
        now_a, now_b = _part(a, role, column), _part(b, role, column)
        if now_a == before.get("b") and now_b == before.get("a"):
            return "retire"
        if now_a != before.get("a") or now_b != before.get("b"):
            return "服事表上這兩天的內容在調換之後又改過了，先照服事表"
        days[ia], days[ib] = _swap(a, b, role, column)
        return "applied"
    return f"不認得的種類「{kind}」"


def overlay(paths, roster: Roster) -> tuple[Roster, dict[str, str]]:  # noqa: ANN001
    """把生效中的調換疊上去。回傳 (疊完的服事表, {調換 id: "applied" / "retired" / 沒套上的原因})。

    服事表已經改好的那幾筆會在這裡標 ``retired`` 並寫回檔案。任何一筆出錯都只跳過那一筆，
    絕不能讓提醒因為調換壞掉而發不出去。
    """
    active = [i for i in load(paths) if i.get("status") == ACTIVE]
    if not active:
        return roster, {}
    days = list(roster.days)
    states: dict[str, str] = {}
    for item in active:
        try:
            result = _apply_one(item, days)
        except (KeyError, ValueError, TypeError, AssertionError) as exc:
            result = f"這一筆的內容看不懂（{exc!r}）"
        states[str(item.get("id"))] = "retired" if result == "retire" else result
    retired = [i for i, s in states.items() if s == "retired"]
    if retired:
        _retire(paths, retired)
    issues = list(roster.issues)
    applied = sum(1 for s in states.values() if s == "applied")
    if applied:
        issues.append(Issue(Severity.INFO, "overrides", f"套用了 {applied} 筆人員調換（服事表本身沒有改）"))
    for item in active:
        state = states[str(item.get("id"))]
        if state not in ("applied", "retired"):
            # INFO 就好：WARNING 會用 LINE 通知管理員、扣額度；這種情況照服事表發就是對的
            issues.append(Issue(Severity.INFO, "override_skipped", f"人員調換「{summary(item)}」這次沒套上：{state}",
                                "服事表那一格改過了，所以先照服事表。不需要這筆了就 swap undo。"))
    return replace(roster, days=tuple(days), issues=tuple(issues)), states


def apply(paths, roster: Roster) -> Roster:  # noqa: ANN001
    return overlay(paths, roster)[0]


def _retire(paths, ids: list[str]) -> None:  # noqa: ANN001
    from church_bot.core import versions

    try:
        with _locked(paths), versions.source("人員調換：服事表已經改好，自動退掉"):
            items = _load_for_write(paths)
            changed = False
            for item in items:
                if str(item.get("id")) in ids and item.get("status") == ACTIVE:
                    item.update(status=RETIRED, retired_at=_now())
                    changed = True
            if changed:
                _save(paths, items)
                log.info("人員調換 %s：服事表已經改成一樣了，自動退掉", "、".join(ids))
    except ChurchBotError as exc:  # 寫不進去下次再退；這次照樣不套（結果一樣）
        log.warning("人員調換自動退掉時寫檔失敗：%s", exc)


# --------------------------------------------------------------------------- 新增、退回


def _append(paths, item: dict) -> dict:  # noqa: ANN001
    with _locked(paths):
        items = _load_for_write(paths)
        for other in items:
            if other.get("status") == ACTIVE and _same(other, item):
                raise SwapError(f"已經有一筆一樣的調換（{other.get('id')}）：{summary(other)}",
                                "不用再加一次；要取消用 swap undo。", existing=str(other.get("id")))
        item["id"] = _new_id(items)
        items.append(item)
        _save(paths, items)
    return item


def _same(a: dict, b: dict) -> bool:
    keys = ("kind", "date", "label", "role", "from", "to", "a", "b", "label_a", "label_b", "column")
    return all(a.get(k, "") == b.get(k, "") for k in keys)


def add_person(paths, roster: Roster, *, date: dt.date, role: str, from_name: str, to_name: str,  # noqa: ANN001
               by: str, reason: str = "", label: str = "", directory: Directory | None = None) -> dict:
    """那一天那一項把 ``from_name`` 換成 ``to_name``。``roster`` 要是疊完調換的（使用者看到的那一份）。"""
    from_name, to_name = (from_name or "").strip(), (to_name or "").strip()
    if not role.strip() or not from_name or not to_name:
        raise SwapError("要給服事項目、原本的人、換成的人（--role、--from、--to）")
    _idx, day = _pick_day(list(roster.days), date, label, "--label")
    a = _role_of(day, role)
    if a is None:
        roles = [x.role for x in day.assignments]
        raise SwapError(f"{when(day.date, day.label)} 沒有「{role}」這一項",
                        f"那一天的服事項目：{'、'.join(roles) or '（沒有）'}", roles=roles)
    raw = _match_name(a.names, from_name, directory)
    if not raw:
        raise SwapError(f"{when(day.date, day.label)} 的「{a.role}」不是「{from_name}」：那一格現在是「{'、'.join(a.names) or '（空白）'}」",
                        "照那一格現在的名字再下一次。", current=list(a.names))
    if _name_key(raw) == _name_key(to_name) or _match_name([to_name], raw, directory):
        raise SwapError(f"「{from_name}」跟「{to_name}」是同一個人，不用換")
    if _match_name(a.names, to_name, directory):
        raise SwapError(f"「{to_name}」已經在 {when(day.date)} 的「{a.role}」裡了", current=list(a.names))
    item = {"id": "", "kind": "person", "at": _now(), "by": by, "reason": reason.strip(), "status": ACTIVE,
            "date": day.date.isoformat(), "label": day.label, "role": a.role, "from": raw, "to": to_name}
    item["summary"] = summary(item)
    return _append(paths, item)


def add_weeks(paths, roster: Roster, *, a: dt.date, b: dt.date, by: str, role: str = "", column: str = "",  # noqa: ANN001
              reason: str = "", label_a: str = "", label_b: str = "") -> dict:
    """兩天對調：整天、只換某一項（``role``）、或只換某一欄（``column``）。"""
    role, column = (role or "").strip(), (column or "").strip()
    if role and column:
        raise SwapError("--role 和 --column 只能給一個（不給 = 整天對調）")
    days = list(roster.days)
    _ia, day_a = _pick_day(days, a, label_a, "--label-a")
    _ib, day_b = _pick_day(days, b, label_b, "--label-b")
    if day_a.date == day_b.date and day_a.label == day_b.label:
        raise SwapError("兩個日期是同一場聚會，沒有東西可以換")
    role_name = col = ""
    if role:
        for day in (day_a, day_b):
            if _role_of(day, role) is None:
                roles = [x.role for x in day.assignments]
                raise SwapError(f"{when(day.date, day.label)} 沒有「{role}」這一項",
                                f"那一天的服事項目：{'、'.join(roles) or '（沒有）'}", roles=roles)
        role_name = _role_of(day_a, role).role  # type: ignore[union-attr]
    elif column:
        col = _column_of([day_a, day_b], column) or _column_of(days, column)
        if not col:
            columns = sorted({k for d in days for k, _ in d.texts})
            raise SwapError(f"服事表沒有「{column}」這個照原樣顯示的欄位",
                            f"現有的：{'、'.join(columns) or '（沒有，到「設定 → 服事表來源 → 照原樣顯示的欄位」設定）'}",
                            columns=columns)
    part_a, part_b = _part(day_a, role_name, col), _part(day_b, role_name, col)
    if part_a == part_b:
        raise SwapError("這兩天這部分的內容一模一樣，換了也沒有差別")
    item = {"id": "", "kind": "weeks", "at": _now(), "by": by, "reason": reason.strip(), "status": ACTIVE,
            "a": day_a.date.isoformat(), "label_a": day_a.label, "b": day_b.date.isoformat(), "label_b": day_b.label,
            "role": role_name, "column": col, "before": {"a": part_a, "b": part_b}}
    item["summary"] = summary(item)
    return _append(paths, item)


def undo(paths, item_id: str, by: str) -> dict:  # noqa: ANN001
    """退回一筆（標 ``undone``，不刪：之後查得到是誰、什麼時候退的）。"""
    item_id = (item_id or "").strip()
    with _locked(paths):
        items = _load_for_write(paths)
        item = next((i for i in items if str(i.get("id")) == item_id), None)
        if item is None:
            raise SwapError(f"找不到編號「{item_id}」的調換", "`swap list --all` 列得出全部的編號。",
                            ids=[str(i.get("id")) for i in items if i.get("status") == ACTIVE])
        if item.get("status") != ACTIVE:
            raise SwapError(f"這一筆已經是「{STATUS_ZH.get(item.get('status', ''), item.get('status'))}」，不用再退",
                            status=item.get("status"))
        item.update(status=UNDONE, undone_at=_now(), undone_by=by)
        _save(paths, items)
    return item


def describe_change(before: bytes | None, after: bytes) -> str:
    """變更紀錄那一行摘要（core/versions.py 呼叫）：新增了哪一筆、哪一筆退回／退掉了。"""
    def parse(data: bytes | None) -> dict[str, dict]:
        if not data:
            return {}
        try:
            raw = json.loads(data.decode("utf-8-sig"))
        except ValueError:
            return {}
        items = raw.get("items") if isinstance(raw, dict) else raw
        return {str(i.get("id")): i for i in items or [] if isinstance(i, dict)}

    old, new = parse(before), parse(after)
    parts = [f"新增調換：{summary(i)}" for k, i in new.items() if k not in old]
    for k, i in new.items():
        if k in old and old[k].get("status") != i.get("status"):
            parts.append(f"{STATUS_ZH.get(i.get('status', ''), i.get('status'))}：{summary(i)}")
    parts += [f"刪除調換：{summary(i)}" for k, i in old.items() if k not in new]
    return "；".join(parts[:3]) + (f"；還有 {len(parts) - 3} 項" if len(parts) > 3 else "") if parts else "修改"
