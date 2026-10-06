"""提醒訊息的「罐頭」寫法：直接打字，要放資料的地方用【中文標籤】，不懂程式的人也改得動。

例如（這就是預設的那一則）：

    📣 【標題】
    📅 【日期】・【聚會】

    ▸ 【服事名單】

    📝 【備註】

    【結尾】

規則只有三條：
1. 【標籤】換成那一次聚會的資料（可以用的見 ``TAGS``）。
2. 【服事名單】那一行會照服事項目重複，一項一行，那一行前後的字每一行都有：
   「▸ 【服事名單】」→「▸ 司琴：小明」「▸ 音控：阿德、小華」…
3. 沒有資料的標籤自動拿掉（連它前面的「・」「、」這類分隔一起拿掉）；整行的標籤都沒有資料，整行拿掉。
   所以「📝 【備註】」在沒有備註的那一週會整行消失，「📅 【日期】・【聚會】」沒有聚會名稱就只剩日期。

舊的 ``{{ title }}`` 寫法（Jinja2）照樣能用：模板裡有 ``{{`` 或 ``{%`` 就照舊的方式排（見 renderer.py）。
"""

from __future__ import annotations

import re

LIST_TAG = "服事名單"
# 標籤 → 說明（給畫面上的「點一下插入」按鈕用，照這個順序排）
TAGS: dict[str, str] = {
    "標題": "設定裡的標題，例如「本週服事提醒」",
    "日期": "聚會日期，例如 10/11（週日）",
    "星期": "只有星期，例如 週日",
    "聚會": "服事表的聚會名稱，例如 主日崇拜（沒有就拿掉）",
    LIST_TAG: "誰服事什麼，一項一行",
    "備註": "服事表的備註（沒有就拿掉）",
    "結尾": "設定裡的結尾文字",
    "群組名稱": "收到這則的 LINE 群組",
}
TAG_RE = re.compile(r"【([^【】\n]{1,12})】")
_SEP = r"・、，,|/／\-–—:：\s"


def is_simple(template: str) -> bool:
    """用中文標籤寫的（新的）；有 {{ 或 {% 的是舊的 Jinja2 寫法。"""
    return "{{" not in template and "{%" not in template


def unknown_tags(template: str) -> list[str]:
    return list(dict.fromkeys(t for t in TAG_RE.findall(template) if t not in TAGS))


def tags_help() -> str:
    return "".join(f"【{t}】" for t in TAGS)


def fill(template: str, values: dict[str, str], rows: list[tuple[str, str]]) -> str:
    """values：標籤 → 資料（服事名單以外）；rows：(服事項目, 名字) 一項一個。回傳還沒整理空行的文字。"""
    out: list[str] = []
    for line in template.replace("\r\n", "\n").split("\n"):
        tags = [t for t in TAG_RE.findall(line) if t in TAGS]
        if not tags:
            out.append(line)
            continue
        if LIST_TAG in tags:
            before, _, after = line.partition(f"【{LIST_TAG}】")
            for role, names in rows:
                out.append(_fill_line(before, values) + f"{role}：{names}" + _fill_line(after, values))
            continue
        if not any(values.get(t) for t in tags):
            continue  # 整行的標籤都沒有資料：整行拿掉
        out.append(_fill_line(line, values))
    return "\n".join(out)


def _fill_line(line: str, values: dict[str, str]) -> str:
    for tag, value in values.items():
        token = f"【{tag}】"
        if token not in line:
            continue
        if value:
            line = line.replace(token, value)
        else:  # 拿掉標籤和它前面的分隔；它在最前面的話，改拿掉後面的分隔
            line = re.sub(rf"[{_SEP}]+{re.escape(token)}|{re.escape(token)}[{_SEP}]*", "", line)
    return line
