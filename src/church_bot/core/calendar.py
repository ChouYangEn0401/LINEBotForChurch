"""「/行事曆」：用 LINE 的 Flex Message 排出「以今天為基準，上兩週＋下四週」的服飾表。

資料來自服事表裡**「照原樣顯示的欄位」**（設定 → 服事表來源 → 照原樣顯示的欄位，例如「服飾」，
見 config.ColumnAliases.text）。那些格子寫的是衣服不是人，所以 parser 不會把它拆成名字、
也不會拿去對同工名單，不然每一格都會跳「服事表上的『深色西裝＋領帶』在同工名單找不到」。

## 為什麼不是圖片

LINE 不收圖片檔，只收網址，由使用者的手機自己去抓。免費模式的 cloudflared 臨時網址每次重開都會變，
舊訊息往上滑就會變破圖。Flex 是 LINE 原生的版型，不需要任何對外網址，也不會過期。

## 怎麼讓它「不跑版」（這一段請不要改掉）

Flex 會跑版幾乎都是這三個原因造成的，所以這裡刻意全部避開：

1. **使用者的字體大小設定**。LINE 的字體大小（小～特大）只有在元件寫了 ``scaling: true`` 時才會跟著變。
   這裡**一律不寫 scaling**，所以不管對方把字體調多大，版面都長一樣。
2. **用 px 指定寬度**。官方明說「用 px 調整整體版面可能會得到意料外的結果，建議改用 flex」。
   這裡沒有任何一個元件用 px 寬度。
3. **水平盒子裡放兩段會變長的文字**。兩邊搶寬度、一邊換行，看起來就是歪的。
   這裡**每一列都是垂直堆疊**（日期那行一行、服飾那行一行），沒有東西需要對齊，所以沒有東西會歪。

另外也刻意**不用 ``adjustMode: shrink-to-fit``**（自動縮字）：官方自己寫它是 best-effort、
「在某些平台可能表現不同，或根本沒作用」，拿它當版面的前提遲早會出事。所有文字一律 ``wrap: true``，
長就換行，換行不會破壞版面。

顏色只是輔助：每一列的底色由服飾文字決定（同樣的服飾永遠同一個顏色），但**文字本身就寫著服飾名稱**，
所以看不出顏色差別也不影響閱讀。
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from typing import Iterable

from church_bot.core.dates import WEEKDAY_CHARS
from church_bot.models import ServiceDay

WEEKS_BACK = 2
WEEKS_FORWARD = 4
MAX_ENTRIES = 20  # 一則 Flex 最多 50 KB；排到這裡就夠長了，再多也沒人看
ALT_TEXT_LIMIT = 400

# 底色（淡）：藍、綠、琥珀、桃、紫、紅。都夠淡，上面放深色字一樣清楚。
# 顏色只是讓人一眼看出「這幾週穿的是同一套」，**不是**用來分類：服飾字本身就寫在上面，
# 所以兩種不同的服飾偶爾撞到同一個底色也不影響閱讀（色號由文字決定，撞色是可能的）。
TINTS = ("#e9f1fc", "#e4f6ef", "#fcf2db", "#fceaf1", "#ebe9f8", "#fcebeb")
PAST_TINT = "#f3f2ef"
HEADER_BG = "#1c5cab"
HEADER_INK = "#ffffff"
HEADER_META = "#cfe0f7"
ACCENT = "#2a78d6"
INK = "#1a1a19"
META = "#6b6a66"
PAST_INK = "#8c8b86"


def week_start(day: dt.date) -> dt.date:
    """那一週的週日（主日排在第一天，教會看比較直覺）。"""
    return day - dt.timedelta(days=(day.weekday() + 1) % 7)


def window(today: dt.date, back: int = WEEKS_BACK, forward: int = WEEKS_FORWARD) -> tuple[dt.date, dt.date]:
    """以今天為基準往前 ``back`` 週、往後 ``forward`` 週，對齊到整週。回傳 (第一天, 最後一天)。"""
    start = week_start(today) - dt.timedelta(weeks=back)
    end = start + dt.timedelta(weeks=back + forward) - dt.timedelta(days=1)
    return start, end


def weekday_zh(day: dt.date) -> str:
    """「三」。注意 WEEKDAY_CHARS 是週一開頭（weekday() 的順序），不是週日開頭。"""
    return WEEKDAY_CHARS[day.weekday()]


@dataclass(frozen=True, slots=True)
class Entry:
    date: dt.date
    label: str
    value: str
    past: bool
    this_week: bool

    @property
    def when(self) -> str:
        return f"{self.date.month}/{self.date.day}（{weekday_zh(self.date)}）"

    def meta(self) -> str:
        parts = [self.when, self.label, "本週" if self.this_week else ""]
        return "ㆍ".join(p for p in parts if p)


def collect(days: Iterable[ServiceDay], column: str, today: dt.date,
            back: int = WEEKS_BACK, forward: int = WEEKS_FORWARD) -> list[Entry]:
    """窗內、這一欄有填東西的日子，由早到晚。同一天兩場聚會就是兩列。"""
    start, end = window(today, back, forward)
    this_week = week_start(today)
    entries = [
        Entry(date=day.date, label=day.label, value=value, past=day.date < today,
              this_week=this_week <= day.date < this_week + dt.timedelta(days=7))
        for day in sorted(days, key=lambda d: d.date)
        if start <= day.date <= end and (value := day.text_of(column))
    ]
    return entries[:MAX_ENTRIES]


def resolve_column(days: Iterable[ServiceDay]) -> str:
    """服事表實際用的欄位名稱。

    設定裡填「服飾」，表頭可能寫「主日服飾」——比對是「包含」就算，所以真正的名字要回頭去資料裡拿。
    設了兩個以上的文字欄位時，用填得最多的那一欄（平手時照名字排，結果才不會每次不一樣）。
    """
    counts: dict[str, int] = {}
    for day in days:
        for name, value in day.texts:
            if value:
                counts[name] = counts.get(name, 0) + 1
    return max(counts, key=lambda name: (counts[name], name), default="")


def _tint(value: str, past: bool) -> str:
    if past:
        return PAST_TINT
    # 同一段服飾文字永遠對到同一個顏色（不用 hash()：那個每次開程式都不一樣）
    return TINTS[hashlib.sha256(value.encode("utf-8")).digest()[0] % len(TINTS)]


def _row(entry: Entry) -> dict:
    """一列 = 垂直堆兩行字。沒有水平盒子，所以沒有東西會對不齊。"""
    tint = _tint(entry.value, entry.past)
    return {
        "type": "box", "layout": "vertical", "spacing": "xs",
        "paddingAll": "12px", "cornerRadius": "8px", "backgroundColor": tint,
        # 每一列都有外框，只是沒輪到的那幾列用跟底色一樣的顏色（看不見）：
        # 這樣每一列的高度完全一樣，本週那一列也不會比別人高一點點。
        "borderWidth": "2px", "borderColor": ACCENT if entry.this_week else tint,
        "contents": [
            {"type": "text", "text": entry.meta(), "size": "xs", "wrap": True,
             "color": PAST_INK if entry.past else META},
            {"type": "text", "text": entry.value, "size": "md", "weight": "bold", "wrap": True,
             "color": PAST_INK if entry.past else INK},
        ],
    }


def _span(start: dt.date, end: dt.date) -> str:
    return f"{start.month}/{start.day} ～ {end.month}/{end.day}"


def flex(entries: list[Entry], today: dt.date, column: str,
         back: int = WEEKS_BACK, forward: int = WEEKS_FORWARD) -> dict:
    """整則 Flex 訊息（可以直接丟給 Reply API）。"""
    start, end = window(today, back, forward)
    span = _span(start, end)
    title = f"{column}表"
    subtitle = f"{span}ㆍ今天 {today.month}/{today.day}（{weekday_zh(today)}）"
    return {
        "type": "flex",
        "altText": f"{title} {span}"[:ALT_TEXT_LIMIT],
        "contents": {
            "type": "bubble", "size": "mega",
            "header": {
                "type": "box", "layout": "vertical", "spacing": "xs",
                "paddingAll": "16px", "backgroundColor": HEADER_BG,
                "contents": [
                    {"type": "text", "text": title, "weight": "bold", "size": "lg",
                     "color": HEADER_INK, "wrap": True},
                    {"type": "text", "text": subtitle, "size": "xs", "color": HEADER_META, "wrap": True},
                ],
            },
            "body": {
                "type": "box", "layout": "vertical", "spacing": "md", "paddingAll": "16px",
                "contents": [
                    *[_row(e) for e in entries],
                    {"type": "text",
                     "text": f"往前 {back} 週、往後 {forward} 週ㆍ資料來自服事表的「{column}」欄",
                     "size": "xxs", "color": PAST_INK, "wrap": True, "margin": "md"},
                ],
            },
        },
    }


def as_text(entries: list[Entry], today: dt.date, column: str,
            back: int = WEEKS_BACK, forward: int = WEEKS_FORWARD) -> str:
    """純文字版：舊版 LINE 顯示不出 Flex 時的備案，也方便複製貼上。"""
    start, end = window(today, back, forward)
    lines = [f"👔 {column}表", f"{_span(start, end)}ㆍ今天 {today.month}/{today.day}（{weekday_zh(today)}）", ""]
    for e in entries:
        mark = "👉 " if e.this_week else ("・" if not e.past else "　")
        lines.append(f"{mark}{e.meta()}\n　{e.value}")
    return "\n".join(lines)
