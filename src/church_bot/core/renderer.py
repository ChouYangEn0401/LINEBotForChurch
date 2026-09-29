"""把一天的服事安排排成訊息文字（Jinja2 模板，可在設定頁修改）。

模板可用的變數：
  title        標題（設定頁填的）
  date_text    排好格式的日期，例如「9/13（週日）」
  date         日期物件；weekday = 「週日」
  label        聚會名稱（服事表有「聚會」欄才有）
  note         備註（服事表有「備註」欄才有）
  footer       結尾文字
  target_name  收訊群組名稱
  assignments  服事清單，每一項有 role（服事項目）、names（已用「、」串好的名字）、people（每個人的詳細資料）

服事表那一格寫的是小團名稱時（見 core/directory.py），names 會排成「晨光實體團（王晨光、陳小明）」，
團裡有登記 LINE 帳號的成員照樣 @ 得到。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass
from typing import Iterable

import jinja2

from church_bot.config import MessageSettings
from church_bot.core.dates import format_date, weekday_zh
from church_bot.core.directory import Directory, normalize_name
from church_bot.errors import ConfigError
from church_bot.models import Assignment, Member, OutgoingMessage, Person, ServiceDay, Target, Team

LINE_TEXT_LIMIT = 5000
MAX_MENTIONS = 20  # LINE 上限更高，但一則提醒 @ 太多人反而是騷擾
_TRUNCATED = "\n…（訊息太長，已截斷）"


def matches_any(text: str, patterns: Iterable[str]) -> bool:
    """「部分符合」：patterns 寫「敬拜」會對到「敬拜主領」「敬拜團」。"""
    t = normalize_name(text)
    return any((p := normalize_name(pat)) and p in t for pat in patterns)


def select_assignments(day: ServiceDay, target: Target | None) -> list[Assignment]:
    items = [a for a in day.assignments if a.names]
    if target and target.roles:
        items = [a for a in items if matches_any(a.role, target.roles)]
    return items


def order_assignments(items: list[Assignment], role_order: list[str]) -> list[Assignment]:
    if not role_order:
        return items

    def rank(pair: tuple[int, Assignment]) -> tuple[int, int]:
        idx, a = pair
        hit = next((i for i, r in enumerate(role_order) if matches_any(a.role, [r])), len(role_order))
        return hit, idx

    return [a for _, a in sorted(enumerate(items), key=rank)]


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _tidy(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    tidy = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if len(tidy) > LINE_TEXT_LIMIT:
        tidy = tidy[: LINE_TEXT_LIMIT - len(_TRUNCATED)] + _TRUNCATED
    return tidy


@dataclass(frozen=True, slots=True)
class _Row:
    role: str
    people: tuple[Person, ...]


def register_hint(rows: list[_Row]) -> str:
    """要 @ 人的群組：列出這次沒辦法 @ 到的人（同工名單沒有他的 LINE_userId），教他們怎麼登記。"""
    # 小團看的是團裡的成員（團名本身不是人，不用登記）
    people = [one for r in rows for p in r.people for one in p.individuals]
    names = [p.display for p in people if not (p.member and p.member.line_user_id)]
    names = list(dict.fromkeys(names))
    if not names:
        return ""
    return (f"🙋 {'、'.join(names)}：還沒辦法 @ 到你，請在這個群組打「/我的ID」，"
            "再打「/我的名字 你的名字」登記，之後提醒就會直接 @ 你 🙏")


class Renderer:
    def __init__(self, cfg: MessageSettings) -> None:
        self.cfg = cfg
        env = jinja2.Environment(undefined=jinja2.StrictUndefined, autoescape=False)
        try:
            self._template = env.from_string(cfg.template)
        except jinja2.TemplateSyntaxError as exc:
            raise ConfigError(
                f"訊息模板第 {exc.lineno} 行有錯：{exc.message}",
                "到「設定」頁按「恢復預設模板」，或檢查 {{ }} 和 {% %} 有沒有成對。",
            ) from exc

    # ------------------------------------------------------------------ public

    def render(self, day: ServiceDay, directory: Directory, target: Target | None = None) -> OutgoingMessage:
        rows = [
            _Row(a.role, tuple(directory.resolve(n) for n in a.names))
            for a in order_assignments(select_assignments(day, target), self.cfg.role_order)
        ]
        text = self._render(day, target, rows, lambda p: p.display)
        if not (target and target.mention):
            return OutgoingMessage(text=text)
        hint = register_hint(rows)
        if hint:
            text = _tidy(f"{text}\n\n{hint}")

        keys: dict[str, str] = {}  # userId → key（同一個人出現兩次共用同一個 key）

        def as_mention(p: Person) -> str:
            uid = p.member.line_user_id if p.member else ""
            if not uid or (uid not in keys and len(keys) >= MAX_MENTIONS):
                return p.display
            keys.setdefault(uid, f"m{len(keys)}")
            return f"\x00{keys[uid]}\x00"

        raw = self._render(day, target, rows, as_mention)
        if hint:
            raw = _tidy(f"{raw}\n\n{hint}")
        if not keys:
            return OutgoingMessage(text=text)
        # textV2：文字裡的 { } 要寫成 {{ }}，佔位符寫成 {key}
        escaped = raw.replace("{", "{{").replace("}", "}}")
        mention_text = re.sub(r"\x00(m\d+)\x00", r"{\1}", escaped)
        return OutgoingMessage(
            text=text, mention_text=mention_text, mentions=tuple((k, uid) for uid, k in keys.items())
        )

    def validate(self) -> None:
        """用假資料試排一次，模板裡打錯變數名稱會在這裡被抓到。"""
        sample = ServiceDay(
            date=dt.date(2026, 9, 13), label="主日崇拜", note="聖餐主日",
            assignments=(Assignment("司琴", ("小明",)), Assignment("音控", ("阿德", "小華")),
                         Assignment("敬拜團", ("示範小團",))),
        )
        directory = Directory([Member(name="王小明", aliases=("小明",), line_user_id="U" + "0" * 32)],
                              [Team(name="示範小團", members=("小明", "阿德"))])
        self.render(sample, directory, Target(name="測試", line_id="", mention=True))

    # ------------------------------------------------------------------ internals

    def _render(self, day: ServiceDay, target: Target | None, rows: list[_Row], fmt) -> str:  # noqa: ANN001
        sep = self.cfg.name_separator

        def show(p: Person) -> str:
            """小團排成「團名（成員…）」；一般名字就是名字（或 @ 的佔位符）。"""
            if p.team is None:
                return fmt(p)
            inside = sep.join(fmt(one) for one in p.team_people)
            return f"{p.display}（{inside}）" if inside else p.display

        context = {
            "title": self.cfg.title,
            "footer": self.cfg.footer,
            "date": day.date,
            "date_text": format_date(day.date, self.cfg.date_format),
            "weekday": weekday_zh(day.date),
            "label": day.label,
            "note": day.note,
            "target_name": target.name if target else "",
            "assignments": [
                {
                    "role": r.role,
                    "names": sep.join(show(p) for p in r.people),
                    "people": [{"name": show(p), "raw": p.raw, "matched": p.matched,
                                "is_team": p.team is not None,
                                "members": [fmt(one) for one in p.team_people]} for p in r.people],
                }
                for r in rows
            ],
        }
        try:
            return _tidy(self._template.render(**context))
        except jinja2.UndefinedError as exc:
            raise ConfigError(
                f"訊息模板用了不存在的變數：{exc.message}",
                "可用的變數：title、date_text、label、note、footer、assignments（role、names）。",
            ) from exc
        except jinja2.TemplateError as exc:
            raise ConfigError(f"訊息模板有錯：{exc}", "到「設定」頁按「恢復預設模板」。") from exc
