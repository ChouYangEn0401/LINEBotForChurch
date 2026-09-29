import datetime as dt

import pytest

from church_bot.core.dates import format_date, infer_year, parse_date, parse_user_date
from tests.conftest import TODAY

SEP13 = dt.date(2026, 9, 13)


@pytest.mark.parametrize(
    "text",
    [
        "2026/9/13", "2026-09-13", "2026.9.13", "2026年9月13日", "2026/09/13 00:00:00",
        "115/9/13", "民國115年9月13日", "26/9/13",
        "9/13", "09/13", "9-13", "9月13日", "9月13號",
        "9/13（日）", "9/13 (週日)", "星期日 9/13", "9/13 聖餐主日",
        "２０２６／９／１３", "Sep 13", "13 Sep 2026", "Sunday, Sep 13, 2026",
        str((SEP13 - dt.date(1899, 12, 30)).days),  # Excel / Google Sheet 日期序號
    ],
)
def test_parse_date_formats(text: str) -> None:
    assert parse_date(text, TODAY) == SEP13


@pytest.mark.parametrize("text", ["", "   ", "十月", "第3-4節", "2026/2/30", "備註", "13/45"])
def test_parse_date_rejects_garbage(text: str) -> None:
    assert parse_date(text, TODAY) is None


def test_year_inference_crosses_year_boundary() -> None:
    assert infer_year(1, 3, dt.date(2026, 12, 28)) == dt.date(2027, 1, 3)
    assert infer_year(12, 28, dt.date(2026, 1, 2)) == dt.date(2025, 12, 28)
    assert infer_year(2, 29, dt.date(2027, 3, 1)) == dt.date(2028, 2, 29)


def test_format_date_tokens() -> None:
    assert format_date(SEP13, "%-m/%-d（{weekday}）") == "9/13（週日）"
    assert format_date(SEP13, "%Y/%m/%d {weekday_long}") == "2026/09/13 星期日"
    assert format_date(SEP13, "民國{roc_year}年%-m月%-d日") == "民國115年9月13日"


# ------------------------------------------------------------------ 人打在 LINE 上的日期（/別周測試）


@pytest.mark.parametrize("text", ["1004", "10/04", "10-4", "10月4日", "2026/10/4", "20261004", "１００４"])
def test_parse_user_date_accepts_what_people_type(text: str) -> None:
    assert parse_user_date(text, TODAY) == dt.date(2026, 10, 4)


def test_parse_user_date_infers_the_nearest_year() -> None:
    assert parse_user_date("904", TODAY) == dt.date(2026, 9, 4)  # 3 位數字 = M + DD
    assert parse_user_date("0103", dt.date(2026, 12, 28)) == dt.date(2027, 1, 3)


@pytest.mark.parametrize("text", ["", "亂打", "10", "1350", "2026/2/30"])
def test_parse_user_date_rejects_garbage(text: str) -> None:
    assert parse_user_date(text, TODAY) is None
