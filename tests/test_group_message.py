"""每個 LINE 群組自己的提醒訊息（標題、結尾、整份模板）；空白沿用牧區設定。"""

import pytest

from church_bot.config import MessageSettings
from church_bot.core.directory import Directory
from church_bot.core.renderer import Renderer
from church_bot.errors import ConfigError
from church_bot.models import Target
from church_bot.tables import TargetTable
from tests.conftest import gid
from tests.test_planner import D13, codes, day, plan

SETTINGS = MessageSettings(title="本週服事提醒", footer="謝謝大家 🙏")
WORSHIP = Target("敬拜團", gid("1"), title="敬拜團這週誰上場", footer="記得週六 7 點練團 🎸")
USHER = Target("招待群", gid("2"))


def render(target):
    return Renderer(SETTINGS).render(day(D13, 司琴=["小明"]), Directory([]), target).text


def test_group_title_and_footer_override_ministry_defaults():
    text = render(WORSHIP)
    assert "敬拜團這週誰上場" in text and "記得週六 7 點練團" in text
    assert "本週服事提醒" not in text and "謝謝大家" not in text


def test_blank_group_fields_fall_back_to_ministry_settings():
    text = render(USHER)
    assert "本週服事提醒" in text and "謝謝大家" in text


def test_group_template_rewrites_whole_message():
    target = Target("禱告群", gid("3"), mention=False, template="🙏 {{ target_name }}：{% for a in assignments %}{{ a.role }}={{ a.names }}{% endfor %}")
    assert render(target) == "🙏 禱告群：司琴=小明"


def test_broken_group_template_names_the_group():
    with pytest.raises(ConfigError) as exc:
        Renderer(SETTINGS).validate(Target("壞群", gid("4"), template="{{ title "))
    assert "壞群" in exc.value.message and "清空" in exc.value.hint
    with pytest.raises(ConfigError, match="壞群"):
        Renderer(SETTINGS).validate(Target("壞群", gid("4"), template="{{ nope }}"))


def test_broken_group_template_only_stops_that_group():
    bad = Target("壞群", gid("4"), template="{{ nope }}")
    p = plan(day(D13, 司琴=["小明"]), targets=(bad, USHER))
    assert [m.target.name for m in p.messages] == ["招待群"]
    assert "target_template" in codes(p) and "target_nothing" not in codes(p)


def test_group_message_roundtrips_through_csv_with_newlines(paths):
    target = Target("禱告群", gid("3"), title="標題", footer="結尾", template="第一行\n{{ title }}\n第三行")
    table = TargetTable(paths.targets_file)
    table.save([target])
    assert table.load().items == [target]
    assert target.has_own_message and not USHER.has_own_message
