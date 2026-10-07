"""「/行事曆」：照原樣顯示的欄位（服飾）、上兩週＋下四週的視窗、Flex 版型不會跑版。"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from church_bot.config import Settings, SourceSettings, save_settings
from church_bot.core import calendar
from church_bot.core.parser import parse_roster
from church_bot.models import RawSheet
from church_bot.service import BotService
from tests.conftest import write
from tests.line_fakes import FakeLine, say, sign

TODAY = dt.date(2026, 9, 11)  # 星期五。本週的週日是 9/6，所以視窗是 8/23 ～ 10/3
TZ = ZoneInfo("Asia/Taipei")

ROSTER = """日期,聚會,講員,司琴,服飾,備註
2026/8/23,主日崇拜,王牧師,小明,便服（深色系）,
2026/8/30,主日崇拜,李傳道,美華,白上衣＋黑長褲,
2026/9/6,主日崇拜,王牧師,小明,深色西裝＋領帶,
2026/9/13,聖餐主日,王牧師,美華,深色西裝＋領帶,聖餐
2026/9/20,主日崇拜,李傳道,小明,便服（深色系）,
2026/10/11,主日崇拜,王牧師,美華,視窗外不該出現,
"""


def roster_of(sheet: str, **columns: list[str]):
    cfg = SourceSettings()
    for key, value in columns.items():
        setattr(cfg.columns, key, value)
    rows = [line.split(",") for line in sheet.strip().splitlines()]
    return parse_roster(RawSheet(rows=rows, source="測試"), cfg, TODAY)


def day_on(roster, month: int, day: int):
    return next(d for d in roster.days if d.date == dt.date(2026, month, day))


# ------------------------------------------------------------------ 照原樣顯示的欄位（parser）


def test_text_column_is_kept_whole_and_never_becomes_a_name():
    roster = roster_of(ROSTER, text=["服飾"])
    sunday = day_on(roster, 9, 13)
    # 「＋」平常是分隔名字用的，文字欄位不會被它拆開
    assert sunday.text_of("服飾") == "深色西裝＋領帶"
    assert "服飾" not in [a.role for a in sunday.assignments]
    assert "深色西裝＋領帶" not in roster.all_names()  # 不會被拿去對同工名單


def test_without_the_setting_the_column_is_still_treated_as_a_role():
    """沒設定就跟以前一樣：服飾被當成一項服事，而且「＋」會把它拆成兩個「人」。"""
    sunday = day_on(roster_of(ROSTER), 9, 13)
    assert sunday.text_of("服飾") == ""
    names = next(a.names for a in sunday.assignments if a.role == "服飾")
    assert names == ("深色西裝", "領帶")


LONG = """日期,服事項目,人員
2026/9/13,講員,王牧師
2026/9/13,服飾,深色西裝＋領帶
2026/9/20,服飾,便服（深色系）
"""

MATRIX = """服事,2026/9/13,2026/9/20
講員,王牧師,李傳道
服飾,深色西裝＋領帶,便服（深色系）
"""


@pytest.mark.parametrize("sheet", [LONG, MATRIX], ids=["long", "matrix"])
def test_text_column_works_in_every_layout(sheet):
    roster = roster_of(sheet, text=["服飾"])
    assert day_on(roster, 9, 13).text_of("服飾") == "深色西裝＋領帶"
    assert day_on(roster, 9, 20).text_of("服飾") == "便服（深色系）"
    assert "深色西裝＋領帶" not in roster.all_names()


def test_resolve_column_returns_the_header_actually_used():
    """設定填「服飾」，表頭寫「主日服飾」也對得上——顯示時要用表頭的寫法。"""
    roster = roster_of(ROSTER.replace("服飾", "主日服飾"), text=["服飾"])
    assert calendar.resolve_column(roster.days) == "主日服飾"


# ------------------------------------------------------------------ 視窗


def test_weekday_characters_are_right():
    """WEEKDAY_CHARS 是週一開頭，別套成週日開頭的索引（會整排差一天）。"""
    assert calendar.weekday_zh(dt.date(2026, 10, 7)) == "三"   # 星期三
    assert calendar.weekday_zh(dt.date(2026, 9, 13)) == "日"   # 主日
    assert calendar.weekday_zh(TODAY) == "五"                  # 2026/9/11 星期五


def test_entry_shows_the_right_weekday():
    entries = calendar.collect(roster_of(ROSTER, text=["服飾"]).days, "服飾", TODAY)
    assert entries[-1].meta().startswith("9/20（日）ㆍ主日崇拜")


def test_window_is_whole_weeks_starting_on_sunday():
    start, end = calendar.window(TODAY)
    assert (start, end) == (dt.date(2026, 8, 23), dt.date(2026, 10, 3))
    assert start.weekday() == 6 and (end - start).days + 1 == 42  # 週日開頭、整整 6 週
    assert start <= TODAY <= end


def test_collect_marks_past_and_this_week_and_drops_whats_outside():
    entries = calendar.collect(roster_of(ROSTER, text=["服飾"]).days, "服飾", TODAY)
    assert [(e.date.month, e.date.day) for e in entries] == [(8, 23), (8, 30), (9, 6), (9, 13), (9, 20)]
    assert [e.past for e in entries] == [True, True, True, False, False]
    # 本週 = 9/6 那一週（9/6 ～ 9/12）；9/13 已經是下一週
    assert [e.this_week for e in entries] == [False, False, True, False, False]


def test_collect_skips_days_where_the_column_is_blank():
    sheet = ROSTER.replace("2026/9/13,聖餐主日,王牧師,美華,深色西裝＋領帶,聖餐",
                           "2026/9/13,聖餐主日,王牧師,美華,,聖餐")
    entries = calendar.collect(roster_of(sheet, text=["服飾"]).days, "服飾", TODAY)
    assert dt.date(2026, 9, 13) not in [e.date for e in entries]


# ------------------------------------------------------------------ Flex 版型：不會跑版


def walk(node):
    """把 Flex JSON 裡每一個元件都走過一遍。"""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item)


@pytest.fixture
def built():
    entries = calendar.collect(roster_of(ROSTER, text=["服飾"]).days, "服飾", TODAY)
    return calendar.flex(entries, TODAY, "服飾")


def test_flex_never_scales_with_the_users_font_size(built):
    """沒寫 scaling 就不會跟著使用者的字體大小變，版面在誰的手機上都一樣。"""
    assert not [n for n in walk(built) if "scaling" in n]


def test_flex_never_sets_a_width_or_height(built):
    """官方建議不要用 px 調版面；這裡乾脆完全不指定寬高，交給 LINE 自己排。"""
    assert not [n for n in walk(built) if {"width", "height"} & n.keys()]


def test_flex_has_no_horizontal_boxes(built):
    """跑版幾乎都來自水平盒子裡兩段文字搶寬度。全部垂直堆疊就沒有東西會歪。"""
    assert not [n for n in walk(built) if n.get("type") == "box" and n.get("layout") != "vertical"]


def test_every_piece_of_text_wraps(built):
    """長的服飾名稱只會換行，不會被切掉、也不會把版面撐開。"""
    texts = [n for n in walk(built) if n.get("type") == "text"]
    assert texts and all(n.get("wrap") is True for n in texts)
    assert not [n for n in texts if "adjustMode" in n]  # 官方說 best-effort，不拿它當前提


def test_rows_are_all_the_same_shape(built):
    """本週那一列也有外框（只是跟底色同色），所以每一列高度一樣。"""
    rows = [n for n in walk(built) if n.get("type") == "box" and "borderWidth" in n]
    assert len(rows) == 5
    assert {n["borderWidth"] for n in rows} == {"2px"}
    highlighted = [n for n in rows if n["borderColor"] != n["backgroundColor"]]
    assert len(highlighted) == 1 and highlighted[0]["borderColor"] == calendar.ACCENT


def test_same_attire_always_gets_the_same_colour():
    """顏色由服飾文字決定，所以每次、每台機器都一樣（不是用 Python 的 hash()，那個每次開程式都會變）。

    刻意**不保證**不同服飾一定不同色：色號只是裝飾，服飾名稱本來就印在上面。
    """
    assert calendar._tint("深色西裝＋領帶", False) == calendar._tint("深色西裝＋領帶", False)
    assert calendar._tint("深色西裝＋領帶", False) in calendar.TINTS
    assert calendar._tint("深色西裝＋領帶", past=True) == calendar.PAST_TINT


def test_past_rows_are_greyed_out(built):
    rows = [n for n in walk(built) if n.get("type") == "box" and "borderWidth" in n]
    past = [n for n in rows if n["backgroundColor"] == calendar.PAST_TINT]
    assert len(past) == 3  # 8/23、8/30、9/6
    assert all(n["contents"][1]["color"] == calendar.PAST_INK for n in past)


def test_alt_text_says_what_it_is(built):
    assert built["altText"].startswith("服飾表 8/23 ～ 10/3")


# ------------------------------------------------------------------ /行事曆 指令


@pytest.fixture
def bot(handler, paths, monkeypatch):
    write(paths.config_dir / "roster.csv", ROSTER)
    settings = Settings()
    settings.source.kind = "csv"
    settings.source.csv_path = "roster.csv"
    settings.source.columns.text = ["服飾"]
    settings.schedule.enabled = False
    save_settings(paths, settings)
    monkeypatch.setattr(BotService, "now",
                        lambda self, s: dt.datetime(2026, 9, 11, 20, 0, tzinfo=TZ))
    return handler


def ask(bot, text: str = "/行事曆") -> None:
    body = say(text)
    bot.handle(body, sign(body))


@pytest.mark.parametrize("command", ["/行事曆", "/服飾", "/calendar", "／日曆"])
def test_calendar_command_replies_with_a_flex_bubble(bot, command):
    ask(bot, command)
    assert len(FakeLine.flex) == 1
    bubble = FakeLine.flex[0]
    assert bubble["type"] == "flex"
    values = [n["text"] for n in walk(bubble) if n.get("type") == "text"]
    assert "深色西裝＋領帶" in values
    assert "視窗外不該出現" not in values


def test_calendar_says_what_to_do_when_the_column_is_not_set_up(handler, paths, monkeypatch):
    write(paths.config_dir / "roster.csv", ROSTER)
    settings = Settings()
    settings.source.kind = "csv"
    settings.source.csv_path = "roster.csv"
    save_settings(paths, settings)  # 沒有設定「照原樣顯示的欄位」
    monkeypatch.setattr(BotService, "now", lambda self, s: dt.datetime(2026, 9, 11, 20, 0, tzinfo=TZ))
    ask(handler)
    assert FakeLine.flex == []
    assert "還沒設定哪一欄是服飾" in FakeLine.replies[-1][1]


def test_calendar_says_so_when_those_weeks_are_blank(bot, paths):
    blank = "\n".join(line for line in ROSTER.splitlines() if "2026/10/11" in line or line.startswith("日期"))
    write(paths.config_dir / "roster.csv", blank + "\n")
    ask(bot)
    assert FakeLine.flex == []
    assert "都是空的" in FakeLine.replies[-1][1]
