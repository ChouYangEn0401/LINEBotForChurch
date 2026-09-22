import datetime as dt

import pytest

from church_bot.config import ColumnAliases, SourceSettings
from church_bot.core.parser import parse_roster, split_names
from church_bot.errors import SourceError
from church_bot.models import RawSheet
from tests.conftest import TODAY

D13, D20 = dt.date(2026, 9, 13), dt.date(2026, 9, 20)


def parse(rows, **cfg):
    return parse_roster(RawSheet(rows=rows, source="test"), SourceSettings(**cfg), TODAY)


def roles(day):
    return {a.role: a.names for a in day.assignments}


def test_wide_with_title_row_label_note_and_empty_marker():
    roster = parse([
        ["2026 第四季服事表"],
        [],
        ["日期", "聚會", "講員", "司琴", "音控", "備註"],
        ["9/13", "主日崇拜", "王牧師", "小明、美華", "-", "聖餐主日"],
        ["9/20", "主日崇拜", "李傳道", "小明", "阿豪", ""],
    ])
    assert [d.date for d in roster.days] == [D13, D20]
    first = roster.days[0]
    assert first.label == "主日崇拜" and first.note == "聖餐主日"
    assert roles(first) == {"講員": ("王牧師",), "司琴": ("小明", "美華"), "音控": ()}
    assert any(i.code == "layout" and "wide" in i.message for i in roster.issues)


def test_wide_merged_date_cells_carry_forward():
    roster = parse([["日期", "聚會", "講員"], ["9/13", "第一堂", "王牧師"], ["", "第二堂", "李傳道"]])
    assert [(d.date, d.label) for d in roster.days] == [(D13, "第一堂"), (D13, "第二堂")]


def test_wide_unreadable_rows_are_reported_not_fatal():
    roster = parse([["日期", "講員"], ["9/13", "王牧師"], ["某一天", "李傳道"], ["十月", ""]])
    assert [d.date for d in roster.days] == [D13]
    unreadable = [i for i in roster.issues if i.code == "row_unreadable"]
    assert len(unreadable) == 1 and "某一天" in unreadable[0].message


def test_wide_without_date_header_uses_first_column():
    roster = parse([["", "講員", "司琴"], ["9/13", "王牧師", "小明"]])
    assert roles(roster.days[0]) == {"講員": ("王牧師",), "司琴": ("小明",)}


def test_ignored_columns_are_dropped():
    roster = parse([["日期", "講員", "經文"], ["9/13", "王牧師", "約翰福音 3:16"]],
                   columns=ColumnAliases(ignore=["經文"]))
    assert roles(roster.days[0]) == {"講員": ("王牧師",)}


def test_long_layout_with_carry_forward():
    roster = parse([
        ["日期", "服事項目", "服事人員"],
        ["9/13", "司琴", "小明"],
        ["", "音控", "阿豪"],
        ["", "", "美華"],
        ["9/20", "司琴", "美華"],
    ])
    assert [d.date for d in roster.days] == [D13, D20]
    assert roles(roster.days[0]) == {"司琴": ("小明",), "音控": ("阿豪", "美華")}
    assert any("long" in i.message for i in roster.issues if i.code == "layout")


def test_matrix_layout():
    roster = parse([
        ["服事", "9/13", "9/20"],
        ["講員", "王牧師", "李傳道"],
        ["司琴", "小明", "美華"],
        ["備註", "聖餐", ""],
    ])
    assert [d.date for d in roster.days] == [D13, D20]
    assert roles(roster.days[0]) == {"講員": ("王牧師",), "司琴": ("小明",)}
    assert roster.days[0].note == "聖餐"


def test_missing_date_column_is_a_clear_error():
    with pytest.raises(SourceError) as exc:
        parse([["名字", "司琴"], ["小明", "美華"]])
    assert "日期" in exc.value.message and exc.value.hint


def test_empty_sheet_is_a_clear_error():
    with pytest.raises(SourceError):
        parse([[], ["", ""]])


def test_forced_long_without_columns_errors():
    with pytest.raises(SourceError):
        parse([["日期", "講員"], ["9/13", "王牧師"]], layout="long")


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        ("小明、美華", ("小明", "美華")),
        ("小明 美華", ("小明", "美華")),
        ("小明/美華\n阿豪", ("小明", "美華", "阿豪")),
        ("John Chen", ("John Chen",)),
        ("小明(主領、吉他)", ("小明(主領、吉他)",)),
        ("小明、小明", ("小明",)),
        ("  ", ()),
        ("-", ()),
        ("無", ()),
    ],
)
def test_split_names(cell, expected):
    assert split_names(cell, SourceSettings().empty_markers) == expected


def test_bookkeeping_columns_and_dash_note_are_dropped():
    """實際教會服事表的樣子：最左邊空一欄、有「月份」「週次」、備註寫「-」。"""
    roster = parse([
        ["", "月份", "週次", "日期", "特別活動/備註", "會前禱", "招待", "回應司琴"],
        ["", "10月", "第一週", "10/4", "-", "以諾", "子安、欣妍", "-"],
        ["", "10月", "第二週", "10/11", "惠如姐團主崇", "晨光", "柏翰", "-"],
    ])
    first, second = roster.days
    assert first.date == dt.date(2026, 10, 4) and first.note == ""
    assert roles(first) == {"會前禱": ("以諾",), "招待": ("子安", "欣妍"), "回應司琴": ()}
    assert second.note == "惠如姐團主崇"
