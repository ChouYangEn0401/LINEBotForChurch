"""罐頭訊息的【中文標籤】寫法：規則，以及跟舊的 Jinja2 預設排出來一模一樣（防重複不會多發）。"""

import datetime as dt

import pytest

from church_bot.config import DEFAULT_TEMPLATE, LEGACY_DEFAULT_TEMPLATE, MessageSettings, Settings
from church_bot.core import message_template as mt
from church_bot.core.directory import Directory
from church_bot.core.renderer import Renderer
from church_bot.errors import ConfigError
from church_bot.models import Assignment, Member, ServiceDay, Target, Team
from tests.conftest import gid, uid

MEMBERS = [Member("陳小明", ("小明",), line_user_id=uid("1")), Member("林美華", ("美華",))]
TEAMS = [Team("晨光實體團", ("晨光",), ("小明", "美華"))]
ROLES = (Assignment("講員", ("王牧師",)), Assignment("司琴", ("小明",)), Assignment("敬拜", ("晨光",)),
         Assignment("音控", ()))


def day(label="", note=""):
    return ServiceDay(dt.date(2026, 10, 11), ROLES, label=label, note=note)


@pytest.mark.parametrize("label", ["", "主日崇拜"])
@pytest.mark.parametrize("note", ["", "聖餐主日"])
@pytest.mark.parametrize("footer", ["", "謝謝大家的擺上 🙏"])
@pytest.mark.parametrize("target", [None, Target("同工群", gid(), mention=True),
                                    Target("敬拜團", gid("c"), roles=("司琴", "敬拜"), title="🎸 敬拜團", footer="練團")])
def test_new_default_renders_exactly_like_the_old_one(label, note, footer, target):
    directory = Directory(MEMBERS, TEAMS)
    new = Renderer(MessageSettings(footer=footer)).render(day(label, note), directory, target)
    old = Renderer(MessageSettings.model_construct(**{**MessageSettings(footer=footer).model_dump(),
                                                      "template": LEGACY_DEFAULT_TEMPLATE})
                   ).render(day(label, note), directory, target)
    assert (new.text, new.mention_text, new.mentions) == (old.text, old.mention_text, old.mentions)


def test_settings_with_the_old_default_are_upgraded():
    assert Settings.model_validate({"message": {"template": LEGACY_DEFAULT_TEMPLATE}}).message.template == DEFAULT_TEMPLATE
    custom = "{{ title }}\n{% for a in assignments %}{{ a.role }}{% endfor %}"
    assert Settings.model_validate({"message": {"template": custom}}).message.template == custom  # 改過的不動


def test_tag_rules():
    values = {"標題": "本週", "日期": "10/11（週日）", "星期": "週日", "聚會": "", "備註": "", "結尾": "",
              "群組名稱": "敬拜團"}
    rows = [("司琴", "小明"), ("音控", "阿德、小華")]
    template = "【標題】｜【群組名稱】\n📅 【日期】・【聚會】\n🎵 【服事名單】 🎵\n📝 【備註】\n【聚會】・【日期】\n直接打的字"
    assert mt.fill(template, values, rows).split("\n") == [
        "本週｜敬拜團", "📅 10/11（週日）", "🎵 司琴：小明 🎵", "🎵 音控：阿德、小華 🎵", "10/11（週日）", "直接打的字"]


def test_unknown_tags_are_explained():
    with pytest.raises(ConfigError) as exc:
        Renderer(MessageSettings(template="【標提】\n【服事名單】"))
    assert "【標提】" in exc.value.message and "【標題】" in exc.value.hint
    with pytest.raises(ConfigError, match="敬拜團"):
        Renderer(MessageSettings()).validate(Target("敬拜團", gid(), template="【名單】"))


def test_group_message_in_tags():
    target = Target("禱告群", gid(), mention=False, template="🙏 【群組名稱】這週：\n・【服事名單】\n【結尾】")
    text = Renderer(MessageSettings(footer="")).render(day(), Directory(MEMBERS, TEAMS), target).text
    assert text.split("\n") == ["🙏 禱告群這週：", "・講員：王牧師", "・司琴：陳小明", "・敬拜：晨光實體團（陳小明、林美華）"]
