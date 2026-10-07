"""把一天的服事安排排成訊息文字（模板可在設定頁、每個群組修改）。

模板有兩種寫法：
* 【中文標籤】（預設、推薦）：不懂程式的人也改得動，規則見 core/message_template.py。
* 舊的 Jinja2（有 {{ 或 {% 的）：照舊能用，可用的變數如下。

Jinja2 模板可用的變數：
  title        標題（群組自己有填就用群組的，沒填用牧區設定頁的）
  date_text    排好格式的日期，例如「9/13（週日）」
  date         日期物件；weekday = 「週日」
  label        聚會名稱（服事表有「聚會」欄才有）
  note         備註（服事表有「備註」欄才有）
  footer       結尾文字（同上：群組優先）
  target_name  收訊群組名稱
  assignments  服事清單，每一項有 role（服事項目）、names（已用「、」串好的名字）、people（每個人的詳細資料）

每個 LINE 群組可以有自己的提醒訊息（標題、結尾文字、整份模板），空白的部分沿用牧區設定；
所以同一天的服事，敬拜團群和招待群可以收到語氣不一樣的罐頭訊息。

服事表那一格寫的是小團名稱時（見 core/directory.py），names 會排成「晨光實體團（王晨光、陳小明）」，
團裡有登記 LINE 帳號的成員照樣 @ 得到。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass, replace
from typing import Iterable

import jinja2

from church_bot.config import MessageSettings
from church_bot.core import message_template
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
    # 一步就好：「/我的名字」那一則訊息本身就帶著是誰打的（LINE 帳號），不用先打「/我的ID」
    return (f"🙋 {'、'.join(names)}：還沒辦法 @ 到你，請在這個群組打「/我的名字 你的名字」登記，"
            "管理員確認後提醒就會直接 @ 你 🙏")


def sample_day() -> tuple[ServiceDay, Directory]:
    """範例的一場聚會（檢查模板、服事表還沒資料時的預覽用）。"""
    day = ServiceDay(
        date=dt.date(2026, 9, 13), label="主日崇拜", note="聖餐主日",
        assignments=(Assignment("司琴", ("小明",)), Assignment("音控", ("阿德", "小華")),
                     Assignment("敬拜團", ("示範小團",))),
    )
    directory = Directory([Member(name="王小明", aliases=("小明",), line_user_id="U" + "0" * 32)],
                          [Team(name="示範小團", members=("小明", "阿德"))])
    return day, directory


class Renderer:
    def __init__(self, cfg: MessageSettings) -> None:
        self.cfg = cfg
        self._env = jinja2.Environment(undefined=jinja2.StrictUndefined, autoescape=False)
        self._template = self._compile(cfg.template)
        self._own: dict[str, jinja2.Template | None] = {}  # 群組自己的模板，同一份內容只檢查／編譯一次

    def _compile(self, source: str, owner: str = "") -> jinja2.Template | None:
        """中文標籤寫法：檢查標籤都認得，回傳 None；舊的 Jinja2 寫法：編譯。寫錯一律丟 ConfigError。"""
        if message_template.is_simple(source):
            if bad := message_template.unknown_tags(source):
                where = f"「{owner}」自己的提醒訊息" if owner else "提醒訊息"
                raise ConfigError(f"{where}裡有不認得的標籤：{'、'.join(f'【{t}】' for t in bad)}",
                                  f"可以用的有：{message_template.tags_help()}。打錯字的話改掉，或點畫面上的按鈕插入。")
            return None
        try:
            return self._env.from_string(source)
        except jinja2.TemplateSyntaxError as exc:
            if owner:
                raise ConfigError(
                    f"「{owner}」自己的訊息模板第 {exc.lineno} 行有錯：{exc.message}",
                    f"到「LINE 群組」頁編輯「{owner}」：修正模板，或把它清空改用牧區的設定。",
                ) from exc
            raise ConfigError(
                f"訊息模板第 {exc.lineno} 行有錯：{exc.message}",
                "到「設定」頁按「恢復預設模板」，或檢查 {{ }} 和 {% %} 有沒有成對。",
            ) from exc

    def _template_for(self, target: Target | None) -> jinja2.Template | None:
        if not (target and target.template):
            return self._template
        if target.template not in self._own:
            self._own[target.template] = self._compile(target.template, target.name)
        return self._own[target.template]

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

    def validate(self, target: Target | None = None) -> None:
        """用假資料試排一次，模板裡打錯變數名稱會在這裡被抓到。``target`` = 順便檢查這個群組自己的訊息。"""
        sample, directory = sample_day()
        trial = replace(target, roles=(), labels=(), mention=True) if target else Target(name="測試", line_id="")
        self.render(sample, directory, trial)

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
            "title": (target.title if target and target.title else self.cfg.title),
            "footer": (target.footer if target and target.footer else self.cfg.footer),
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
        owner = target.name if target and target.template else ""
        compiled = self._template_for(target)
        if compiled is None:  # 中文標籤寫法
            values = {"標題": context["title"], "日期": context["date_text"], "星期": context["weekday"],
                      "聚會": context["label"], "備註": context["note"], "結尾": context["footer"],
                      "群組名稱": context["target_name"]}
            rows = [(a["role"], a["names"]) for a in context["assignments"]]
            source = target.template if owner else self.cfg.template
            return _tidy(message_template.fill(source, values, rows))
        where = f"「{owner}」自己的訊息模板" if owner else "訊息模板"
        fix = f"到「LINE 群組」頁編輯「{owner}」，或把它的模板清空改用牧區的設定。" if owner else "到「設定」頁按「恢復預設模板」。"
        try:
            return _tidy(compiled.render(**context))
        except jinja2.UndefinedError as exc:
            raise ConfigError(
                f"{where}用了不存在的變數：{exc.message}",
                f"可用的變數：title、date_text、label、note、footer、target_name、assignments（role、names）。{fix}",
            ) from exc
        except jinja2.TemplateError as exc:
            raise ConfigError(f"{where}有錯：{exc}", fix) from exc
