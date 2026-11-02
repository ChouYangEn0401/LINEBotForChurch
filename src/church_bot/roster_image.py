"""把「看服事表」（roster_view.py）畫成一張 PNG：給 ClawBot 傳到 Telegram（`/line` 面板的「📅 服事表」）。

一列一天：左邊日期＋星期＋聚會名、中間每一項服事和人（一格一項，三格一排）、右邊照原樣顯示的那一欄（服飾）。
被人員調換改過的格子是**黃底＋「※」**，最下面一行寫這是什麼意思。本週那一列有藍框，過去的灰掉。

刻意簡單：寬度固定 1080px（手機上剛好一個螢幕寬）、字夠大、不用 matplotlib（慢又大），只用 Pillow。
中文字型找 Windows 的微軟正黑體（msjh）→ 微軟雅黑（msyh）→ 細明體（mingliu）；都找不到就用 Pillow 內建的，
那種字型沒有中文（會變方塊），所以結果的 ``font`` 會是 ``"fallback"``，呼叫的人看得出來。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from church_bot.core import calendar
from church_bot.roster_view import DayView, RosterView

WIDTH = 1080
MARGIN = 28
PAD = 20
GAP = 12
DATE_W = 170
TEXT_W = 230
CELLS_PER_ROW = 3

BG = "#f6f5f2"
CARD = "#ffffff"
PAST_CARD = "#f0efeb"
ADJUSTED_BG = "#fdf0c4"
ADJUSTED_EDGE = "#d9a400"
CELL_BG = "#f4f6fa"
TEXT_BG = "#e9f1fc"  # 照原樣顯示的那一欄（服飾）：淡藍，跟服事項目的灰底分得開、跟「已調整」的黃底也分得開
MARK = "※"


@dataclass(frozen=True, slots=True)
class Fonts:
    name: str  # 用了哪個字型檔（msjh、msyh…）；"fallback" = Pillow 內建、沒有中文
    title: ImageFont.ImageFont
    subtitle: ImageFont.ImageFont
    date: ImageFont.ImageFont
    small: ImageFont.ImageFont
    names: ImageFont.ImageFont
    text: ImageFont.ImageFont


def _font_dirs() -> list[Path]:
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
    return [Path(windir) / "Fonts", Path("/System/Library/Fonts"), Path("/usr/share/fonts/opentype/noto"),
            Path("/usr/share/fonts/truetype/noto")]


CANDIDATES = (  # (名字, 一般, 粗體)
    ("msjh", "msjh.ttc", "msjhbd.ttc"),
    ("msyh", "msyh.ttc", "msyhbd.ttc"),
    ("mingliu", "mingliu.ttc", "mingliub.ttc"),
    ("pingfang", "PingFang.ttc", "PingFang.ttc"),
    ("noto-cjk", "NotoSansCJK-Regular.ttc", "NotoSansCJK-Bold.ttc"),
)


def find_font() -> tuple[str, Path, Path] | None:
    for folder in _font_dirs():
        for name, regular, bold in CANDIDATES:
            if (folder / regular).exists():
                heavy = folder / bold
                return name, folder / regular, heavy if heavy.exists() else folder / regular
    return None


def load_fonts() -> Fonts:
    found = find_font()
    if found is not None:
        name, regular, bold = found
        try:
            def r(size: int) -> ImageFont.FreeTypeFont:
                return ImageFont.truetype(str(regular), size)

            def b(size: int) -> ImageFont.FreeTypeFont:
                return ImageFont.truetype(str(bold), size)

            return Fonts(name, b(44), r(26), b(40), r(22), b(28), r(26))
        except OSError:
            pass
    d = ImageFont.load_default
    return Fonts("fallback", d(size=44), d(size=26), d(size=40), d(size=22), d(size=28), d(size=26))


# --------------------------------------------------------------------------- 量字、換行


def _width(font: ImageFont.ImageFont, text: str) -> float:
    return font.getlength(text) if text else 0.0


def _line_h(font: ImageFont.ImageFont) -> int:
    top, bottom = font.getbbox("國Ag")[1], font.getbbox("國Ag")[3]
    return int((bottom - top) * 1.3) + 2


def wrap(text: str, font: ImageFont.ImageFont, width: float) -> list[str]:
    """一個字一個字排（中文沒有空白可以斷），超過寬度就換行；原本的換行照留。"""
    lines: list[str] = []
    for paragraph in (text or "").split("\n"):
        line = ""
        for ch in paragraph:
            if line and _width(font, line + ch) > width:
                lines.append(line)
                line = ch
            else:
                line += ch
        lines.append(line)
    return lines or [""]


# --------------------------------------------------------------------------- 版面


@dataclass(slots=True)
class _Cell:
    label: str
    lines: list[str]
    adjusted: bool
    height: int


def _cells(day: DayView, fonts: Fonts, cell_w: float) -> list[_Cell]:
    out = []
    inner = cell_w - 2 * 10
    for slot in day.slots:
        label = f"{MARK} {slot.role}" if slot.adjusted else slot.role
        lines = wrap("、".join(slot.names) or "—", fonts.names, inner)
        out.append(_Cell(label, lines, slot.adjusted, 10 + _line_h(fonts.small) + len(lines) * _line_h(fonts.names) + 8))
    return out


def _layout(view: RosterView, fonts: Fonts) -> tuple[float, float, list[tuple[DayView, list[_Cell], list[str], int]]]:
    has_text = bool(view.column)
    grid_w = WIDTH - 2 * MARGIN - 2 * PAD - DATE_W - GAP - ((TEXT_W + GAP) if has_text else 0)
    cell_w = (grid_w - GAP * (CELLS_PER_ROW - 1)) / CELLS_PER_ROW
    rows = []
    for day in view.days:
        cells = _cells(day, fonts, cell_w)
        grid_h = 0
        for i in range(0, len(cells), CELLS_PER_ROW):
            grid_h += max(c.height for c in cells[i:i + CELLS_PER_ROW]) + GAP
        grid_h = max(grid_h - GAP, 0)
        value = day_text(day, view.column)
        text_lines = wrap(value or "—", fonts.text, TEXT_W - 24) if has_text else []
        text_h = (12 + _line_h(fonts.small) + len(text_lines) * _line_h(fonts.text) + 10) if has_text else 0
        label_lines = wrap(day.label, fonts.small, DATE_W - 8) if day.label else []
        date_h = _line_h(fonts.date) + _line_h(fonts.subtitle) + len(label_lines) * _line_h(fonts.small)
        note_h = _line_h(fonts.small) * len(wrap(day.note, fonts.small, grid_w)) + 6 if day.note else 0
        height = max(date_h, grid_h + note_h, text_h) + 2 * PAD
        rows.append((day, cells, text_lines, height))
    return grid_w, cell_w, rows


def day_text(day: DayView, column: str) -> str:
    return next((v for k, v in day.texts if k == column), "")


def render(view: RosterView, ministry: str = "") -> tuple[Image.Image, str]:
    """畫好的圖、用了哪個字型。"""
    fonts = load_fonts()
    grid_w, cell_w, rows = _layout(view, fonts)
    header_h = 30 + _line_h(fonts.title) + _line_h(fonts.subtitle) + 26
    legend = f"{MARK} 黃底＝有人員調換（服事表本身沒有改）　藍框＝本週　資料來源：{view.source}"
    legend_lines = wrap(legend, fonts.small, WIDTH - 2 * MARGIN)
    body_h = sum(h + GAP for *_rest, h in rows) if rows else 120
    height = header_h + MARGIN + body_h + len(legend_lines) * _line_h(fonts.small) + MARGIN

    img = Image.new("RGB", (WIDTH, int(height)), BG)
    draw = ImageDraw.Draw(img)

    # 標題
    draw.rectangle([0, 0, WIDTH, header_h], fill=calendar.HEADER_BG)
    title = f"{ministry}　服事表" if ministry else "服事表"
    draw.text((MARGIN, 26), title, font=fonts.title, fill=calendar.HEADER_INK)
    t = view.today
    sub = (f"{view.start.month}/{view.start.day} ～ {view.end.month}/{view.end.day}　"
           f"今天 {t.month}/{t.day}（{calendar.weekday_zh(t)}）")
    draw.text((MARGIN, 30 + _line_h(fonts.title)), sub, font=fonts.subtitle, fill=calendar.HEADER_META)

    y = header_h + MARGIN
    if not rows:
        draw.text((MARGIN, y + 30), "這段時間服事表上沒有聚會", font=fonts.subtitle, fill=calendar.META)
        y += 120
    for day, cells, text_lines, h in rows:
        _draw_row(draw, fonts, view, day, cells, text_lines, y, h, grid_w, cell_w)
        y += h + GAP
    for line in legend_lines:
        draw.text((MARGIN, y), line, font=fonts.small, fill=calendar.META)
        y += _line_h(fonts.small)
    return img, fonts.name


def _draw_row(draw: ImageDraw.ImageDraw, fonts: Fonts, view: RosterView, day: DayView, cells: list[_Cell],
              text_lines: list[str], y: float, h: int, grid_w: float, cell_w: float) -> None:
    ink = calendar.PAST_INK if day.past else calendar.INK
    meta = calendar.PAST_INK if day.past else calendar.META
    left, right = MARGIN, WIDTH - MARGIN
    draw.rounded_rectangle([left, y, right, y + h], radius=14, fill=PAST_CARD if day.past else CARD,
                           outline=calendar.ACCENT if day.this_week else "#e2e0da", width=4 if day.this_week else 1)
    # 日期
    x, cy = left + PAD, y + PAD
    draw.text((x, cy), f"{day.date.month}/{day.date.day}", font=fonts.date, fill=ink)
    cy += _line_h(fonts.date)
    week = f"週{day.weekday}" + ("　本週" if day.this_week else "")
    draw.text((x, cy), week, font=fonts.subtitle, fill=calendar.ACCENT if day.this_week else meta)
    cy += _line_h(fonts.subtitle)
    for line in wrap(day.label, fonts.small, DATE_W - 8) if day.label else []:
        draw.text((x, cy), line, font=fonts.small, fill=meta)
        cy += _line_h(fonts.small)
    # 服事項目：一格一項
    gx = left + PAD + DATE_W + GAP
    gy = y + PAD
    for i in range(0, len(cells), CELLS_PER_ROW):
        band = cells[i:i + CELLS_PER_ROW]
        band_h = max(c.height for c in band)
        for j, cell in enumerate(band):
            cx = gx + j * (cell_w + GAP)
            draw.rounded_rectangle([cx, gy, cx + cell_w, gy + band_h], radius=10,
                                   fill=ADJUSTED_BG if cell.adjusted else CELL_BG,
                                   outline=ADJUSTED_EDGE if cell.adjusted else None, width=2 if cell.adjusted else 0)
            draw.text((cx + 10, gy + 8), cell.label, font=fonts.small, fill=ADJUSTED_EDGE if cell.adjusted else meta)
            ly = gy + 8 + _line_h(fonts.small)
            for line in cell.lines:
                draw.text((cx + 10, ly), line, font=fonts.names, fill=ink)
                ly += _line_h(fonts.names)
        gy += band_h + GAP
    if day.note:
        for line in wrap(day.note, fonts.small, grid_w):
            draw.text((gx, gy), line, font=fonts.small, fill=meta)
            gy += _line_h(fonts.small)
    # 照原樣顯示的那一欄（服飾）
    if view.column:
        value = day_text(day, view.column)
        adjusted = view.column in day.adjusted_texts
        tx = right - PAD - TEXT_W
        # 不用 /行事曆 那幾個依內容變的底色：其中有一個淡黃色，會跟「已調整」的黃底搞混
        tint = ADJUSTED_BG if adjusted else TEXT_BG
        box_h = 12 + _line_h(fonts.small) + len(text_lines) * _line_h(fonts.text) + 10
        draw.rounded_rectangle([tx, y + PAD, tx + TEXT_W, y + PAD + box_h], radius=10, fill=tint,
                               outline=ADJUSTED_EDGE if adjusted else None, width=2 if adjusted else 0)
        label = f"{MARK} {view.column}" if adjusted else view.column
        draw.text((tx + 12, y + PAD + 8), label, font=fonts.small, fill=ADJUSTED_EDGE if adjusted else meta)
        ly = y + PAD + 8 + _line_h(fonts.small)
        for line in text_lines:
            draw.text((tx + 12, ly), line, font=fonts.text, fill=ink)
            ly += _line_h(fonts.text)


def save(view: RosterView, out: Path, ministry: str = "") -> str:
    """畫好存成 PNG（先寫暫存檔再改名，對方不會讀到一半的檔案）。回傳用了哪個字型。"""
    img, font = render(view, ministry)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    img.save(tmp, format="PNG", optimize=True)
    tmp.replace(out)
    return font
