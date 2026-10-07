"""每個 LINE 群組自己的提醒訊息（標題、結尾、整份模板）；空白沿用牧區設定。"""

import pytest

from church_bot.config import MessageSettings
from church_bot.core.directory import Directory
from church_bot.core.renderer import Renderer
from church_bot.errors import ConfigError
from church_bot.models import Target
from church_bot.tables import TargetTable
from tests.conftest import CHURCH, gid
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


# ------------------------------------------------------------------ 管理網頁「LINE 群組」頁


def test_web_saves_group_message_and_shows_preview(client, paths):
    r = client.post("/targets/save", data={"name": "敬拜團", "line_id": gid("1"), "enabled": "on",
                                           "title": "敬拜團這週誰上場", "footer": "記得練團"}, follow_redirects=False)
    assert r.status_code == 303 and "edit=" in r.headers["location"]
    saved = next(t for t in TargetTable(paths.targets_file).load().items if t.name == "敬拜團")
    assert (saved.title, saved.footer, saved.template) == ("敬拜團這週誰上場", "記得練團", "")
    page = client.get(CHURCH + r.headers["location"]).text
    assert "群組會收到" in page and 'data-insert-tag="【服事名單】"' in page and 'value="敬拜團這週誰上場"' in page


def test_live_preview_uses_what_is_typed_before_saving(client):
    """打字的時候預覽就跟著變（還沒按儲存）；空白的部分用牧區設定。"""
    group = {"preview_kind": "target", "name": "敬拜團", "roles": "司琴", "title": "🎸 這週誰上場", "footer": "",
             "template": ""}
    data = client.post("/api/message-preview", data=group).json()
    assert data["error"] == "" and data["text"].startswith("📣 🎸 這週誰上場") and "司琴：" in data["text"]
    assert "講員" not in data["text"]  # 照這個群組的「只發這些服事」
    data = client.post("/api/message-preview", data={**group, "template": "🙏【群組名稱】\n・【服事名單】"}).json()
    assert data["text"].startswith("🙏敬拜團\n・司琴：")
    data = client.post("/api/message-preview", data={**group, "template": "【標提】"}).json()
    assert "【標提】" in data["error"] and data["text"] == ""


def test_live_preview_for_the_ministry_settings(client):
    data = client.post("/api/message-preview", data={"title": "青年崇拜", "footer": "", "template": ""}).json()
    assert data["text"].startswith("📣 青年崇拜") and "謝謝大家" not in data["text"] and data["note"].startswith("用 ")


def test_web_keeps_typed_template_when_it_is_broken(client, paths):
    r = client.post("/targets/save", data={"name": "壞群", "line_id": gid("4"), "enabled": "on",
                                           "template": "我打了很久的內容 {{ title "})
    assert r.status_code == 200 and "還沒儲存" in r.text and "我打了很久的內容" in r.text
    assert all(t.name != "壞群" for t in TargetTable(paths.targets_file).load().items)
