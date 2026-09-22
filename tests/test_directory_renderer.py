import datetime as dt

import pytest

from church_bot.config import MessageSettings
from church_bot.core.directory import Directory
from church_bot.core.renderer import Renderer
from church_bot.errors import ConfigError
from church_bot.models import Assignment, Member, ServiceDay, Target
from tests.conftest import gid, uid

MEMBERS = [
    Member("陳小明", ("小明", "Ming")),
    Member("林美華", ("美華",)),
    Member("周以琳", ("以琳",), active=False),
]
DAY = ServiceDay(
    dt.date(2026, 9, 13),
    (Assignment("講員", ("王牧師",)), Assignment("司琴", ("小明",)), Assignment("音控", ())),
    label="主日崇拜",
    note="聖餐主日",
)


# ------------------------------------------------------------------ directory


def test_resolve_by_alias_width_and_case():
    d = Directory(MEMBERS)
    assert d.resolve("小明").display == "陳小明"
    assert d.resolve("ｍｉｎｇ").display == "陳小明"
    assert d.resolve(" 美 華 ").display == "林美華"


def test_resolve_keeps_annotation():
    assert Directory(MEMBERS).resolve("小明(代)").display == "陳小明(代)"


def test_unknown_name_gets_suggestions():
    person = Directory(MEMBERS).resolve("小明明")
    assert not person.matched and person.display == "小明明"
    assert "陳小明" in person.suggestions
    assert Directory(MEMBERS).resolve("陌生人").suggestions == ()


# ------------------------------------------------------------------ renderer


def test_default_template():
    text = Renderer(MessageSettings()).render(DAY, Directory(MEMBERS)).text
    assert "9/13（週日）・主日崇拜" in text
    assert "▸ 講員：王牧師" in text and "▸ 司琴：陳小明" in text
    assert "音控" not in text  # 沒排人的項目不出現
    assert "📝 聖餐主日" in text


def test_role_filter_and_order():
    renderer = Renderer(MessageSettings())
    only_music = renderer.render(DAY, Directory([]), Target("敬拜團", gid(), roles=("司琴",))).text
    assert "司琴" in only_music and "講員" not in only_music
    ordered = Renderer(MessageSettings(role_order=["司琴"])).render(DAY, Directory([])).text
    assert ordered.index("司琴") < ordered.index("講員")


def test_mentions_use_text_v2_placeholders_and_escape_braces():
    directory = Directory([Member("陳小明", ("小明",), line_user_id=uid())])
    template = "{note} {{ title }}\n{% for a in assignments %}{{ a.role }}={{ a.names }}\n{% endfor %}"
    message = Renderer(MessageSettings(template=template)).render(DAY, directory, Target("g", gid(), mention=True))
    assert message.has_mentions and message.mentions == (("m0", uid()),)
    assert "司琴=陳小明" in message.text
    assert "司琴={m0}" in message.mention_text
    assert message.mention_text.startswith("{{note}}")  # 文字裡本來的大括號要跳脫


def test_no_mentions_without_user_id():
    message = Renderer(MessageSettings()).render(DAY, Directory(MEMBERS), Target("g", gid(), mention=True))
    assert not message.has_mentions


def test_template_errors_are_config_errors():
    with pytest.raises(ConfigError):
        Renderer(MessageSettings(template="{% for a in %}"))
    with pytest.raises(ConfigError) as exc:
        Renderer(MessageSettings(template="{{ titel }}")).validate()
    assert "titel" in exc.value.message


def test_people_who_cannot_be_tagged_are_told_how_to_register():
    directory = Directory([Member("陳小明", ("小明",), line_user_id=uid())])
    message = Renderer(MessageSettings()).render(DAY, directory, Target("g", gid(), mention=True))
    assert message.has_mentions  # 陳小明有 userId → @ 他
    for text in (message.text, message.mention_text):
        assert "王牧師：還沒辦法 @ 到你" in text and "/我的ID" in text and "/我的名字" in text
    assert "陳小明：" not in message.text


def test_no_register_hint_when_group_does_not_tag():
    text = Renderer(MessageSettings()).render(DAY, Directory([]), Target("g", gid(), mention=False)).text
    assert "/我的ID" not in text


def test_two_character_names_do_not_get_noisy_fuzzy_guesses():
    """兩個字的名字只要共用一個字，difflib 分數就會壓線通過，容易亂猜（例如「國良」被猜成「蔡建國」）。
    子字串包含（去掉姓的暱稱，例如「以諾」→「王以諾」）不受影響，只有「字形相近」的猜測改成三個字以上才猜。
    """
    d = Directory([Member("蔡建國", ("建國", "蔡弟兄")), Member("許心怡", ("心怡",)), Member("王以諾", ())])
    assert d.resolve("國良").suggestions == ()  # 跟「建國」只差共用一個「國」字，不該被猜到
    assert d.resolve("雅心").suggestions == ()  # 跟「心怡」只差共用一個「心」字，不該被猜到
    assert d.resolve("以諾").suggestions == ("王以諾",)  # 去掉姓的暱稱：子字串包含，還是要猜得到
