"""人員調換（覆蓋層，core/overrides.py）與 ``swap`` 指令：套用、自動退掉、退回、對不上時說清楚。"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from church_bot.church import add_ministry
from church_bot.core import overrides
from church_bot.core.renderer import ADJUSTED_MARK
from church_bot.ministries import Church
from tests.cli_setup import ROSTER, fix_now, run, setup_ministry
from tests.conftest import gid, write

SEP13 = dt.date(2026, 9, 13)
SEP20 = dt.date(2026, 9, 20)


@pytest.fixture
def church(paths, monkeypatch):
    setup_ministry(paths)
    fix_now(monkeypatch)
    return Church(paths.church)


def roster_of(church):
    service = church.service("m1")
    ctx = service.load()
    return service.fetch_roster(ctx.settings, service.now(ctx.settings).date())


def day(roster, date):
    return next(d for d in roster.days if d.date == date)


def names(d, role):
    return next(a.names for a in d.assignments if a.role == role)


def preview_texts(church):
    report, _plan = church.service("m1").preview()
    return {d.target_name: d.text for d in report.deliveries}


# ------------------------------------------------------------------ swap person


def test_swap_person_is_applied_everywhere_the_roster_is_read(church, paths, capsys):
    code, out = run(paths.church, "swap", "person", "--date=2026-09-13", "--role=司琴", "--from=小明", "--to=美華",
                    "--reason=小明出國", "--by=telegram:42", capsys=capsys)
    assert code == 0, out
    assert out["ok"] and out["id"] and "小明 → 美華" in out["summary"]
    assert out["ministry"] == {"id": "m1", "name": "測試牧區"}

    sunday = day(roster_of(church), SEP13)
    assert names(sunday, "司琴") == ("美華",) and sunday.adjusted_on("司琴") and not sunday.adjusted_on("講員")
    texts = preview_texts(church)  # 每週推播、管理網頁的預覽都走同一條路
    assert "林美華" in texts["司琴群"] and "陳小明" not in texts["司琴群"]
    assert texts["同工群"].count(ADJUSTED_MARK) == 1  # 那一天標一次就好，不是每個名字都標

    saved = json.loads((paths.data_dir / "overrides.json").read_text(encoding="utf-8"))["items"][0]
    assert saved["by"] == "telegram:42" and saved["reason"] == "小明出國" and saved["status"] == "active"


def test_swap_person_accepts_the_display_name_too(church, paths, capsys):
    code, out = run(paths.church, "swap", "person", "--date=2026-09-13", "--role=司琴", "--from=陳小明", "--to=林美華",
                    "--by=telegram:42", capsys=capsys)
    assert code == 0, out
    assert out["item"]["from"] == "小明"  # 記服事表上的寫法：之後比對 Sheet 才對得上


def test_swap_person_from_not_in_that_cell_says_who_is_there(church, paths, capsys):
    code, out = run(paths.church, "swap", "person", "--date=2026-09-13", "--role=司琴", "--from=建國", "--to=美華",
                    "--by=telegram:42", capsys=capsys)
    assert code == 1 and not out["ok"]
    assert "小明" in out["message"] and out["current"] == ["小明"]
    assert not (paths.data_dir / "overrides.json").exists()


def test_swap_unknown_date_or_role(church, paths, capsys):
    code, out = run(paths.church, "swap", "person", "--date=2026-09-14", "--role=司琴", "--from=小明", "--to=美華",
                    "--by=telegram:42", capsys=capsys)
    assert code == 1 and "沒有" in out["message"] and "2026-09-13" in out["nearby"]
    code, out = run(paths.church, "swap", "person", "--date=2026-09-13", "--role=鼓手", "--from=小明", "--to=美華",
                    "--by=telegram:42", capsys=capsys)
    assert code == 1 and "司琴" in out["roles"]
    code, out = run(paths.church, "swap", "person", "--date=9/13", "--role=司琴", "--from=小明", "--to=美華",
                    "--by=telegram:42", capsys=capsys)
    assert code == 1 and "YYYY-MM-DD" in out["hint"]


def test_writes_need_by(church, paths, capsys):
    code, out = run(paths.church, "swap", "person", "--date=2026-09-13", "--role=司琴", "--from=小明", "--to=美華",
                    capsys=capsys)
    assert code == 1 and "--by" in out["message"]
    code, out = run(paths.church, "swap", "undo", "--id=abc", capsys=capsys)
    assert code == 1 and "--by" in out["message"]


def test_the_same_swap_twice_is_refused(church, paths, capsys):
    argv = ("swap", "person", "--date=2026-09-13", "--role=司琴", "--from=小明", "--to=美華", "--by=telegram:42")
    assert run(paths.church, *argv, capsys=capsys)[0] == 0
    code, out = run(paths.church, *argv, capsys=capsys)
    # 第二次：那一格已經是美華了（看到的是疊完的），所以「小明」對不上
    assert code == 1 and out["current"] == ["美華"]


# ------------------------------------------------------------------ swap weeks


def test_swap_weeks_whole_day(church, paths, capsys):
    code, out = run(paths.church, "swap", "weeks", "--a=2026-09-13", "--b=2026-09-20", "--by=telegram:42", capsys=capsys)
    assert code == 0, out
    roster = roster_of(church)
    a, b = day(roster, SEP13), day(roster, SEP20)
    assert names(a, "講員") == ("李傳道",) and names(b, "講員") == ("王牧師",)
    assert a.text_of("服飾") == "便服" and b.text_of("服飾") == "深色西裝"
    assert a.label == "主日崇拜" and a.date == SEP13  # 日期、聚會名不動
    assert a.adjusted_on("講員") and a.adjusted_on("服飾")


def test_swap_weeks_one_role(church, paths, capsys):
    code, out = run(paths.church, "swap", "weeks", "--a=2026-09-13", "--b=2026-09-20", "--role=招待",
                    "--by=telegram:42", capsys=capsys)
    assert code == 0, out
    roster = roster_of(church)
    assert names(day(roster, SEP13), "招待") == ("建國",)
    assert names(day(roster, SEP20), "招待") == ("喜樂", "恩典")
    assert names(day(roster, SEP13), "講員") == ("王牧師",)  # 別的項目不動
    assert day(roster, SEP13).adjusted == ("招待",)


def test_swap_weeks_one_column_shows_on_the_calendar(church, paths, capsys):
    code, out = run(paths.church, "swap", "weeks", "--a=2026-09-13", "--b=2026-09-27", "--column=服飾",
                    "--by=telegram:42", capsys=capsys)
    assert code == 0, out
    entries, column, _today, _note = church.service("m1").calendar_now()  # /行事曆
    by_date = {e.date: e for e in entries}
    assert column == "服飾"
    assert by_date[SEP13].value == "白上衣" and by_date[SEP13].adjusted and "已調整" in by_date[SEP13].meta()
    assert by_date[dt.date(2026, 9, 27)].value == "深色西裝"
    assert not by_date[SEP20].adjusted


def test_swap_weeks_errors(church, paths, capsys):
    code, out = run(paths.church, "swap", "weeks", "--a=2026-09-13", "--b=2026-10-04", "--by=telegram:42", capsys=capsys)
    assert code == 1 and "10/4" in out["message"]
    code, out = run(paths.church, "swap", "weeks", "--a=2026-09-13", "--b=2026-09-20", "--column=髮型",
                    "--by=telegram:42", capsys=capsys)
    assert code == 1 and out["columns"] == ["服飾"]
    code, out = run(paths.church, "swap", "weeks", "--a=2026-09-06", "--b=2026-09-13", "--role=講員",
                    "--by=telegram:42", capsys=capsys)
    assert code == 1 and "一模一樣" in out["message"]  # 兩天都是王牧師
    code, out = run(paths.church, "swap", "weeks", "--a=2026-09-06", "--b=2026-09-13", "--role=講員", "--column=服飾",
                    "--by=telegram:42", capsys=capsys)
    assert code == 1


# ------------------------------------------------------------------ 自動退掉、對不上、退回


def test_sheet_fixed_by_hand_retires_the_swap(church, paths, capsys):
    code, out = run(paths.church, "swap", "person", "--date=2026-09-13", "--role=司琴", "--from=小明", "--to=美華",
                    "--by=telegram:42", capsys=capsys)
    assert code == 0
    # 有人把 Google Sheet 改成一樣了
    write(paths.config_dir / "roster.csv", ROSTER.replace("2026/9/13,主日崇拜,王牧師,小明", "2026/9/13,主日崇拜,王牧師,美華"))
    sunday = day(roster_of(church), SEP13)
    assert names(sunday, "司琴") == ("美華",) and not sunday.is_adjusted  # 照 Sheet，不再標已調整
    item = overrides.load(paths)[0]
    assert item["status"] == "retired" and item["retired_at"]
    code, listed = run(paths.church, "swap", "list", capsys=capsys)
    assert code == 0 and listed["items"] == []
    code, listed = run(paths.church, "swap", "list", "--all", capsys=capsys)
    assert [i["status"] for i in listed["items"]] == ["retired"]


def test_weeks_swap_retires_when_the_sheet_is_swapped_by_hand(church, paths, capsys):
    run(paths.church, "swap", "weeks", "--a=2026-09-13", "--b=2026-09-20", "--role=司琴", "--by=telegram:42", capsys=capsys)
    swapped = (ROSTER.replace("2026/9/13,主日崇拜,王牧師,小明", "2026/9/13,主日崇拜,王牧師,美華")
               .replace("2026/9/20,主日崇拜,李傳道,美華", "2026/9/20,主日崇拜,李傳道,小明"))
    write(paths.config_dir / "roster.csv", swapped)
    roster = roster_of(church)
    assert names(day(roster, SEP13), "司琴") == ("美華",) and not day(roster, SEP13).is_adjusted
    assert overrides.load(paths)[0]["status"] == "retired"


def test_sheet_changed_to_someone_else_is_not_overridden(church, paths, capsys):
    run(paths.church, "swap", "person", "--date=2026-09-13", "--role=司琴", "--from=小明", "--to=美華",
        "--by=telegram:42", capsys=capsys)
    write(paths.config_dir / "roster.csv", ROSTER.replace("2026/9/13,主日崇拜,王牧師,小明", "2026/9/13,主日崇拜,王牧師,建國"))
    assert names(day(roster_of(church), SEP13), "司琴") == ("建國",)  # 以 Sheet 為準
    code, listed = run(paths.church, "swap", "list", capsys=capsys)
    row = listed["items"][0]
    assert row["status"] == "active" and row["applied"] is False and "建國" in row["note"]


def test_undo(church, paths, capsys):
    _code, out = run(paths.church, "swap", "person", "--date=2026-09-13", "--role=司琴", "--from=小明", "--to=美華",
                     "--by=telegram:42", capsys=capsys)
    code, undone = run(paths.church, "swap", "undo", f"--id={out['id']}", "--by=telegram:7", capsys=capsys)
    assert code == 0 and undone["status"] == "undone"
    assert names(day(roster_of(church), SEP13), "司琴") == ("小明",)
    item = overrides.load(paths)[0]
    assert item["undone_by"] == "telegram:7"
    code, again = run(paths.church, "swap", "undo", f"--id={out['id']}", "--by=telegram:7", capsys=capsys)
    assert code == 1 and again["status"] == "undone"
    code, missing = run(paths.church, "swap", "undo", "--id=nope", "--by=telegram:7", capsys=capsys)
    assert code == 1 and "nope" in missing["message"]


def test_swaps_are_in_the_change_log(church, paths, capsys):
    run(paths.church, "swap", "person", "--date=2026-09-13", "--role=司琴", "--from=小明", "--to=美華",
        "--by=telegram:42", capsys=capsys)
    changes = church.versions.changes("m1")
    assert changes[0].file == "data/ministries/m1/overrides.json" and changes[0].label == "人員調換"
    assert "小明 → 美華" in changes[0].summary and changes[0].source == "Telegram（ClawBot）"


def test_a_broken_overrides_file_never_stops_reminders(church, paths):
    write(paths.data_dir / "overrides.json", "{壞掉")
    assert names(day(roster_of(church), SEP13), "司琴") == ("小明",)
    assert "同工群" in preview_texts(church)


def test_overrides_reach_the_line_reminder_command(church, paths, capsys):
    """LINE 群組打「/提醒」（service.notify_now）看到的也是調換後的。"""
    run(paths.church, "swap", "person", "--date=2026-09-13", "--role=司琴", "--from=小明", "--to=美華",
        "--by=telegram:42", capsys=capsys)
    messages, _note = church.service("m1").notify_now(gid("c"))
    assert any("林美華" in m.text and ADJUSTED_MARK in m.text for m in messages)


# ------------------------------------------------------------------ 哪個牧區


def test_two_ministries_need_ministry(church, paths, capsys):
    add_ministry(paths.church, "壯年牧區")
    code, out = run(paths.church, "swap", "list", capsys=capsys)
    assert code == 1 and out["ministries"] == [{"id": "m1", "name": "測試牧區"}, {"id": "m2", "name": "壯年牧區"}]
    code, out = run(paths.church, "swap", "list", "--ministry=測試牧區", capsys=capsys)
    assert code == 0 and out["ministry"]["id"] == "m1"
    code, out = run(paths.church, "swap", "list", "--ministry=不存在", capsys=capsys)
    assert code == 1 and len(out["ministries"]) == 2


def test_bad_arguments_still_print_json(capsys):
    from church_bot.cli import main

    assert main(["swap", "person", "--date=2026-09-13", "--json"]) == 2
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["ok"] is False and "--role" in out["message"]
