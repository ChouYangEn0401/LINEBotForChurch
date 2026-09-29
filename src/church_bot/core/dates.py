"""日期解析與格式化。

服事表上的日期寫法五花八門，這裡盡量都吃得下：
2026/9/13、2026-09-13、2026年9月13日、115/9/13（民國）、9/13、9月13日、
9/13（日）、9/13 聖餐主日、Sep 13、Excel 日期序號 46278 …

沒有寫年份時，自動挑「離今天最近」的那一年（12 月排隔年 1 月的表也不會錯）。
格式化不用 strftime：Windows 不支援 %-m，而且中文格式字串在不同平台行為不一。
"""

from __future__ import annotations

import datetime as dt
import re

WEEKDAY_CHARS = "一二三四五六日"

_FULLWIDTH = str.maketrans("０１２３４５６７８９／－．：　", "0123456789/-.: ")
_ROC_PREFIX = re.compile(r"^(?:中華)?民國\s*")
_WEEKDAY_NOISE = re.compile(
    r"[（(]\s*(?:週|周|星期|禮拜)?[一二三四五六日天]\s*[)）]"
    r"|(?:週|周|星期|禮拜)[一二三四五六日天]"
    r"|\b(?:mon|tue|wed|thu|fri|sat|sun)(?:day|sday|nesday|rsday|urday)?\b\.?",
    re.IGNORECASE,
)
_YMD = re.compile(r"^(?P<y>\d{2,4})\s*[-/.年]\s*(?P<m>\d{1,2})\s*[-/.月]\s*(?P<d>\d{1,2})(?!\d)")
_MD = re.compile(r"^(?P<m>\d{1,2})\s*[-/.月]\s*(?P<d>\d{1,2})(?!\d)")
_SERIAL = re.compile(r"^\d{5}(?:\.0+)?$")
_EN_WITH_YEAR = ("%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y")
_EN_NO_YEAR = ("%b %d", "%B %d", "%d %b", "%d %B")
_EXCEL_EPOCH = dt.date(1899, 12, 30)


def _safe(y: int, m: int, d: int) -> dt.date | None:
    try:
        return dt.date(y, m, d)
    except ValueError:
        return None


def infer_year(month: int, day: int, today: dt.date) -> dt.date | None:
    """沒寫年份：從去年、今年、明年裡挑離今天最近的合法日期。"""
    candidates = [c for y in (today.year - 1, today.year, today.year + 1) if (c := _safe(y, month, day))]
    return min(candidates, key=lambda c: abs((c - today).days), default=None)


def parse_date(text: str, today: dt.date) -> dt.date | None:
    """看不懂就回傳 None（不丟例外），由呼叫端決定要不要報錯。"""
    s = (text or "").translate(_FULLWIDTH).strip()
    if not s:
        return None
    s = _ROC_PREFIX.sub("", s)

    if _SERIAL.match(s):
        n = int(float(s))
        return _EXCEL_EPOCH + dt.timedelta(days=n) if 20000 < n < 80000 else None

    s = _WEEKDAY_NOISE.sub(" ", s).replace(",", " ").strip()

    if m := _YMD.match(s):
        y = int(m["y"])
        if y < 100:
            y += 2000  # 26/9/13 → 2026
        elif y < 1000:
            y += 1911  # 民國年：115/9/13 → 2026
        return _safe(y, int(m["m"]), int(m["d"]))

    if m := _MD.match(s):
        return infer_year(int(m["m"]), int(m["d"]), today)

    compact = re.sub(r"\s+", " ", s)
    for fmt in _EN_WITH_YEAR:
        try:
            return dt.datetime.strptime(compact, fmt).date()
        except ValueError:
            pass
    for fmt in _EN_NO_YEAR:
        try:
            parsed = dt.datetime.strptime(compact, fmt)
        except ValueError:
            continue
        return infer_year(parsed.month, parsed.day, today)
    return None


_YYYYMMDD = re.compile(r"^(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})$")
_MMDD = re.compile(r"^(?P<m>\d{1,2})(?P<d>\d{2})$")


def parse_user_date(text: str, today: dt.date) -> dt.date | None:
    """人在 LINE 上打的日期：1004、10/04、10月4日、2026/10/4…

    比 ``parse_date`` 多吃「純數字」的寫法（1004 = 10/04、904 = 9/04、20261004 = 2026/10/04），
    因為在手機上打字時懶得打斜線。同樣看不懂就回傳 None。
    """
    s = (text or "").translate(_FULLWIDTH).strip()
    if m := _YYYYMMDD.match(s):
        return _safe(int(m["y"]), int(m["m"]), int(m["d"]))
    if m := _MMDD.match(s):
        return infer_year(int(m["m"]), int(m["d"]), today)
    return parse_date(s, today)


# --------------------------------------------------------------------------- formatting

_TOKENS = {
    "%Y": lambda d: f"{d.year:04d}",
    "%y": lambda d: f"{d.year % 100:02d}",
    "%m": lambda d: f"{d.month:02d}",
    "%d": lambda d: f"{d.day:02d}",
    "%-m": lambda d: str(d.month),
    "%-d": lambda d: str(d.day),
    "{weekday}": lambda d: "週" + WEEKDAY_CHARS[d.weekday()],
    "{weekday_long}": lambda d: "星期" + WEEKDAY_CHARS[d.weekday()],
    "{roc_year}": lambda d: str(d.year - 1911),
}
_TOKEN_RE = re.compile("|".join(re.escape(k) for k in sorted(_TOKENS, key=len, reverse=True)))


def format_date(d: dt.date, fmt: str) -> str:
    return _TOKEN_RE.sub(lambda m: _TOKENS[m.group(0)](d), fmt)


def weekday_zh(d: dt.date) -> str:
    return "週" + WEEKDAY_CHARS[d.weekday()]
