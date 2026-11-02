"""給別的程式呼叫的指令（ClawBot 的 `/line` 面板就是呼叫這些）：``swap``、``roster``、``remind``；
``quota``、``ministries`` 的 ``--json`` 也用這裡的 ``emit_json``。

契約（給人看的完整版在 docs/CLI.md）跟 ``webhook`` 指令一樣：

* 值一律 ``--旗標=值`` 一個 token；``--json`` 時**最後一行**是一份 JSON（stdout 只有這一行，紀錄寫在 stderr）。
* 結束碼 0 = 成功、1 = 沒做成（JSON 的 ``message`` 說原因）、2 = 設定有問題。
* ``--ministry=<編號或名稱>``：只有一個牧區時可以省；兩個以上沒給 → 結束碼 1，``ministries`` 列出有哪些。
* 寫入的指令（``swap person``／``weeks``／``undo``、``remind now``）一定要 ``--by=<誰>``（例如 ``telegram:123``），
  沒給 → 結束碼 1。``by`` 寫進那一筆紀錄。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from typing import TYPE_CHECKING, Callable

from church_bot.core import overrides, versions
from church_bot.core.overrides import SwapError
from church_bot.errors import ChurchBotError, ConfigError

if TYPE_CHECKING:
    from church_bot.config import Paths
    from church_bot.ministries import Church, Unit

BY_LIMIT = 80


class CliFail(Exception):
    """沒做成：結束碼 ``code``（預設 1），``extra`` 放進 JSON。"""

    def __init__(self, message: str, hint: str = "", *, code: int = 1, **extra: object) -> None:
        super().__init__(message)
        self.message, self.hint, self.code, self.extra = message, hint, code, extra


# --------------------------------------------------------------------------- 輸出


def _print(text: str = "") -> None:
    from church_bot.cli import _print as out

    out(text)


def emit_json(payload: object) -> None:
    """印一行 JSON。stdout 是 UTF-8 就照原樣印中文；不是（例如被別的程式用 cp950 接）就用 \\u 跳脫，
    不管對方用什麼編碼接，解出來都一樣。"""
    encoding = (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "").replace("_", "")
    _print(json.dumps(payload, ensure_ascii=encoding not in ("utf8", "utf8sig")))


Result = tuple[int, dict, list[str]]  # (結束碼, JSON, 給人看的幾行字)


def run_json(fn: Callable[["Paths", argparse.Namespace], Result]) -> Callable[["Paths", argparse.Namespace], int]:
    """包一層：錯誤一律變成 ``{"ok": false, "message", "hint", …}``（``--json`` 時）或兩行字。"""

    def command(paths: "Paths", args: argparse.Namespace) -> int:
        try:
            code, payload, lines = fn(paths, args)
        except SwapError as exc:
            code, payload = 1, {"ok": False, "message": exc.message, "hint": exc.hint, **exc.extra}
            lines = _fail_lines(exc.message, exc.hint)
        except CliFail as exc:
            code, payload = exc.code, {"ok": False, "message": exc.message, "hint": exc.hint, **exc.extra}
            lines = _fail_lines(exc.message, exc.hint)
        except ConfigError as exc:
            code, payload = 2, {"ok": False, "message": exc.message, "hint": exc.hint}
            lines = _fail_lines(exc.message, exc.hint)
        except ChurchBotError as exc:  # 讀不到服事表、LINE 連不上…：這次沒做成，不是設定錯
            code, payload = 1, {"ok": False, "message": exc.message, "hint": exc.hint}
            lines = _fail_lines(exc.message, exc.hint)
        if getattr(args, "json", False):
            emit_json(payload)
        else:
            for line in lines:
                _print(line)
        return code

    command.__doc__ = fn.__doc__
    return command


def _fail_lines(message: str, hint: str) -> list[str]:
    return [f"❌ {message}"] + ([f"   → {hint}"] if hint else [])


# --------------------------------------------------------------------------- 共用：哪個牧區、誰


def ministry_list(church: "Church") -> list[dict]:
    return [{"id": m.id, "name": m.name} for m in church.ministries()]


def one_unit(paths: "Paths", args: argparse.Namespace) -> tuple["Church", "Unit"]:
    """``--ministry`` 指定的那一個；沒給而且只有一個牧區就是它；兩個以上沒給 → 結束碼 1 並列出有哪些。"""
    from church_bot.ministries import Church

    church = Church(paths)
    key = (getattr(args, "ministry", "") or paths.ministry or "").strip()
    if key:
        unit = church.unit(key)
        if unit is None:
            raise CliFail(f"找不到牧區「{key}」", "用 --ministry= 給編號（例如 m1）或名稱。",
                          ministries=ministry_list(church))
        return church, unit
    units = church.units()
    if not units:
        raise CliFail("還沒有任何牧區", "打開管理網頁（2-start）新增第一個牧區。", ministries=[])
    if len(units) > 1:
        raise CliFail(f"有 {len(units)} 個牧區，要用 --ministry= 指定是哪一個",
                      "、".join(f"{u.name}（{u.id}）" for u in units), ministries=ministry_list(church))
    return church, units[0]


def require_by(args: argparse.Namespace) -> str:
    by = (getattr(args, "by", "") or "").strip()
    if not by:
        raise CliFail("寫入的指令要用 --by= 說是誰要做的（例如 --by=telegram:123456）")
    if len(by) > BY_LIMIT or any(ord(ch) < 32 for ch in by):
        raise CliFail(f"--by 太長或有看不懂的字（最多 {BY_LIMIT} 個字）")
    return by


def change_source(by: str) -> str:
    """變更紀錄的「來源」（core/versions.py 刻意只記來源、不記是誰；是誰寫在那一筆調換自己的 by 裡）。"""
    return "Telegram（ClawBot）" if by.startswith("telegram:") else "指令列"


def _date(value: str, flag: str) -> dt.date:
    try:
        return dt.date.fromisoformat((value or "").strip())
    except ValueError:
        raise CliFail(f"{flag} 的日期看不懂：「{value}」", "要寫成 YYYY-MM-DD，例如 2026-09-13。") from None


def _ministry_json(unit: "Unit") -> dict:
    return {"id": unit.id, "name": unit.name}


def _current_roster(unit: "Unit"):  # noqa: ANN202 - (Context, today, Roster)
    service = unit.service
    ctx = service.load()
    today = service.now(ctx.settings).date()
    return ctx, today, service.fetch_roster(ctx.settings, today)


# --------------------------------------------------------------------------- swap


def _item_json(item: dict, state: str | None = None) -> dict:
    out = {"id": str(item.get("id", "")), "kind": item.get("kind", ""), "summary": overrides.summary(item),
           "by": item.get("by", ""), "at": item.get("at", ""), "reason": item.get("reason", ""),
           "status": item.get("status", ""), "dates": overrides.dates_of(item)}
    for key in ("role", "column", "from", "to", "label", "label_a", "label_b", "retired_at", "undone_at", "undone_by"):
        if item.get(key):
            out[key] = item[key]
    if state is not None:
        out["applied"] = state == "applied"
        out["note"] = "" if state in ("applied", "retired") else state
    return out


def _swap(paths: "Paths", args: argparse.Namespace) -> Result:
    """人員調換：person（那一天那一項換人）、weeks（兩天對調）、list（看）、undo（退回一筆）。"""
    church, unit = one_unit(paths, args)
    action = args.action
    head = {"ok": True, "ministry": _ministry_json(unit)}
    if action == "list":
        return _swap_list(unit, args, head)
    by = require_by(args)
    if action == "undo":
        with versions.source(change_source(by)):
            item = overrides.undo(unit.service.paths, args.id, by)
        text = f"已退回：{overrides.summary(item)}（{item['id']}）"
        return 0, {**head, "id": item["id"], "summary": overrides.summary(item), "status": item["status"],
                   "message": text}, [f"✅ {text}"]
    ctx, _today, roster = _current_roster(unit)
    with versions.source(change_source(by)):
        if action == "person":
            item = overrides.add_person(unit.service.paths, roster, date=_date(args.date, "--date"), role=args.role or "",
                                        from_name=getattr(args, "from_name", "") or "", to_name=args.to or "", by=by,
                                        reason=args.reason or "", label=args.label or "", directory=ctx.directory)
        else:
            item = overrides.add_weeks(unit.service.paths, roster, a=_date(args.a, "--a"), b=_date(args.b, "--b"), by=by,
                                       role=args.role or "", column=args.column or "", reason=args.reason or "",
                                       label_a=args.label_a or "", label_b=args.label_b or "")
    text = f"已調換：{overrides.summary(item)}（編號 {item['id']}；服事表本身沒改，要退回：swap undo --id={item['id']}）"
    return 0, {**head, "id": item["id"], "summary": overrides.summary(item), "item": _item_json(item, "applied"),
               "message": text}, [f"✅ {text}"]


def _swap_list(unit: "Unit", args: argparse.Namespace, head: dict) -> Result:
    states: dict[str, str] = {}
    roster_error = ""
    try:  # 順便看每一筆現在有沒有套上（要讀服事表；讀不到也照樣列）
        service = unit.service
        ctx = service.load()
        raw = service.fetch_raw_roster(ctx.settings, service.now(ctx.settings).date())
        _roster, states = overrides.overlay(service.paths, raw)
    except ChurchBotError as exc:
        roster_error = exc.message
    items = overrides.load(unit.service.paths)
    shown = items if args.all else [i for i in items if i.get("status") == overrides.ACTIVE]
    rows = [_item_json(i, states.get(str(i.get("id"))) if i.get("status") == overrides.ACTIVE and states else None)
            for i in shown]
    lines = [f"人員調換（{unit.name}）：{'全部' if args.all else '生效中'} {len(rows)} 筆"]
    for row in rows:
        mark = "" if row.get("applied", True) else f"　⚠️ 沒套上：{row.get('note')}"
        status = "" if row["status"] == overrides.ACTIVE else f"［{overrides.STATUS_ZH.get(row['status'], row['status'])}］"
        lines.append(f"・{row['id']}　{status}{row['summary']}　（{row['by']}，{row['at'][:16].replace('T', ' ')}）{mark}")
    if roster_error:
        lines.append(f"（讀不到服事表，所以不知道每一筆現在有沒有套上：{roster_error}）")
    return 0, {**head, "items": rows, "count": len(rows), "all": bool(args.all), "roster_error": roster_error}, lines


cmd_swap = run_json(_swap)


# --------------------------------------------------------------------------- roster


def _roster(paths: "Paths", args: argparse.Namespace) -> Result:
    """看服事表：show（JSON／文字）、image（畫成 PNG）。範圍跟 /行事曆 一樣，疊完人員調換。"""
    from pathlib import Path

    from church_bot import roster_view

    _church, unit = one_unit(paths, args)
    if args.weeks_back < 0 or args.weeks_back > roster_view.MAX_WEEKS_BACK:
        raise CliFail(f"--weeks-back 要在 0 到 {roster_view.MAX_WEEKS_BACK} 之間")
    if args.weeks_forward < 1 or args.weeks_forward > roster_view.MAX_WEEKS_FORWARD:
        raise CliFail(f"--weeks-forward 要在 1 到 {roster_view.MAX_WEEKS_FORWARD} 之間")
    view = roster_view.collect(unit.service, args.weeks_back, args.weeks_forward)
    head = {"ok": True, "ministry": _ministry_json(unit)}
    if args.action == "show":
        return 0, {**head, **view.to_json()}, roster_view.as_text(view, unit.name)
    from church_bot import roster_image

    out = Path(args.out).expanduser()
    if not out.is_absolute():
        out = Path.cwd() / out
    try:
        font = roster_image.save(view, out, unit.name)
    except OSError as exc:
        raise CliFail(f"圖存不進去：{out}（{exc}）", "確認資料夾存在、可以寫入。") from exc
    data = view.to_json()
    payload = {**head, "path": str(out), "days": len(view.days), "font": font, "today": data["today"],
               "window": data["window"], "column": view.column}
    lines = [f"✅ 已畫好 {len(view.days)} 天的服事表：{out}"]
    if font == "fallback":
        lines.append("⚠️ 找不到中文字型，圖上的中文可能是方塊")
    return 0, payload, lines


cmd_roster = run_json(_roster)


# --------------------------------------------------------------------------- 參數


def common(json_flag: bool = True) -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--牧區", "--ministry", "-m", dest="ministry", default="",
                        help="哪個牧區（編號或名稱）；只有一個牧區時可以省")
    parent.add_argument("--by", default="", help="誰要做的（寫入的指令一定要給，例如 telegram:123456）")
    if json_flag:
        parent.add_argument("--json", action="store_true", help="最後一行印一份 JSON（給 ClawBot 之類的程式看）")
    return parent


def add_parsers(sub: argparse._SubParsersAction) -> None:  # noqa: SLF001
    parent = common()
    swap = sub.add_parser("swap", help="人員調換（疊在服事表上，不改 Google Sheet）：person、weeks、list、undo")
    actions = swap.add_subparsers(dest="action", metavar="動作", required=True)
    person = actions.add_parser("person", parents=[parent], help="那一天那一項把某個人換成另一個人")
    person.add_argument("--date", required=True, help="哪一天（YYYY-MM-DD）")
    person.add_argument("--role", required=True, help="服事項目（例如 司琴）")
    person.add_argument("--from", dest="from_name", required=True, help="原本的人（服事表上的寫法或正式名字都可以）")
    person.add_argument("--to", required=True, help="換成誰")
    person.add_argument("--label", default="", help="同一天有兩場聚會時，是哪一場（聚會名）")
    person.add_argument("--reason", default="", help="原因（選填）")
    weeks = actions.add_parser("weeks", parents=[parent], help="兩天對調：整天、只換一項（--role）、或只換一欄（--column）")
    weeks.add_argument("--a", required=True, help="第一天（YYYY-MM-DD）")
    weeks.add_argument("--b", required=True, help="第二天（YYYY-MM-DD）")
    weeks.add_argument("--role", default="", help="只換這一項服事")
    weeks.add_argument("--column", default="", help="只換這一個照原樣顯示的欄位（例如 服飾）")
    weeks.add_argument("--label-a", default="", help="第一天有兩場聚會時，是哪一場")
    weeks.add_argument("--label-b", default="", help="第二天有兩場聚會時，是哪一場")
    weeks.add_argument("--reason", default="", help="原因（選填）")
    listing = actions.add_parser("list", parents=[parent], help="列出生效中的調換（--all 連退回、自動退掉的也列）")
    listing.add_argument("--all", action="store_true", help="連已退回、已自動退掉的也列")
    undo = actions.add_parser("undo", parents=[parent], help="退回一筆調換")
    undo.add_argument("--id", required=True, help="調換的編號（swap list 看得到）")

    roster = sub.add_parser("roster", help="看服事表（上兩週＋下四週，疊完人員調換）：show（文字／JSON）、image（PNG）")
    views = roster.add_subparsers(dest="action", metavar="動作", required=True)
    show = views.add_parser("show", parents=[parent], help="印出來（加 --json 給程式看）")
    image = views.add_parser("image", parents=[parent], help="畫成一張 PNG（寬 1080px）")
    image.add_argument("--out", required=True, help="PNG 存到哪裡")
    for p in (show, image):
        p.add_argument("--weeks-back", type=int, default=2, help="往前幾週（預設 2，跟 /行事曆 一樣）")
        p.add_argument("--weeks-forward", type=int, default=4, help="往後幾週（預設 4）")


COMMANDS = {"swap": cmd_swap, "roster": cmd_roster}
