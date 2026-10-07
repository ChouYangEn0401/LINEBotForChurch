"""登記 → 「全部對應」整條路走一遍，專挑會出事的情況：大家在 LINE 打「/我的名字」，管理員在網頁按「全部對應」。

用同一個管理網頁裡的 Webhook 處理 LINE 訊息（跟實際一樣），LINE 換成 tests/line_fakes.py 的 FakeLine。
"""

from urllib.parse import unquote

import pytest

from church_bot.config import load_settings, save_settings, update_env_file
from church_bot.models import Member, Target
from church_bot.tables import MemberTable, TargetTable
from tests.line_fakes import SECRET, FakeLine, event_body, gid, say, sign, uid

ADMIN = uid("a")


@pytest.fixture
def church(client, paths, monkeypatch):
    update_env_file(paths.env_file, {"LINE_CHANNEL_SECRET": SECRET, "LINE_CHANNEL_ACCESS_TOKEN": "tok"})
    FakeLine.reset()
    monkeypatch.setattr("church_bot.webhook.LineMessenger", FakeLine)
    settings = load_settings(paths)
    settings.chat.collect_names = True
    save_settings(paths, settings)
    TargetTable(paths.targets_file).save([Target("同工群", gid())])
    MemberTable(paths.members_file).save([
        Member("管理員", line_user_id=ADMIN, admin=True),
        Member("陳小明", ("小明", "Ming")),
        Member("林美華", ("美華",)),
        Member("黃喜樂", line_user_id=uid("9")),  # 已經有帳號
        Member("吳恩典", active=False),  # 停用中
    ])
    return client


def line(client, text, user, chat=None):
    body = say(text, user=user, chat=chat or gid())
    client.app.state.webhook.handle(body, sign(body))
    return FakeLine.replies[-1][1]


def members(paths):
    return {m.name: m for m in MemberTable(paths.members_file).load().items}


def link_all(client):
    r = client.post("/members/accounts/link-all", follow_redirects=False)
    return unquote(r.headers["location"])


def test_everyday_mix_of_registrations(church, paths):
    assert "已登記" in line(church, "/我的名字 陳小明", uid("1"))
    line(church, "/我的名字 美華", uid("2"))  # 打的是其他寫法：一樣對得到林美華
    line(church, "/我的名字 黃喜樂", uid("3"))  # 那位已經有帳號了：衝突，不能一次對應
    line(church, "/我的名字 阿德哥", uid("4"))  # 名單上沒有：要管理員「加入」
    line(church, "/我的名字 ｗｕ 恩典", uid("5"))  # 全形、多空白：對不到（名字不一樣），留給管理員
    line(church, "/我的名字 吳恩典", uid("6"))  # 停用的同工：照樣可以對應（他回來服事了）
    page = church.get("/members/accounts").text
    assert "名字對得上的 3 位全部對應" in page

    message = link_all(church)
    assert "已對應 3 位" in message
    found = members(paths)
    assert (found["陳小明"].line_user_id, found["林美華"].line_user_id, found["吳恩典"].line_user_id) == \
        (uid("1"), uid("2"), uid("6"))
    assert found["黃喜樂"].line_user_id == uid("9")  # 沒被蓋掉
    assert "阿德哥" not in found
    assert "已對應 3 位" not in link_all(church) and "沒有可以一次對應" in link_all(church)  # 再按一次不會出事

    remaining = church.get("/members/accounts").text
    assert "阿德哥" in remaining and "黃喜樂" in remaining  # 剩下的照舊一個一個處理


def test_two_people_claiming_the_same_person_are_left_for_the_admin(church, paths):
    line(church, "/我的名字 陳小明", uid("1"))
    line(church, "/我的名字 Ming", uid("2"))  # 另一個人說自己也是陳小明（用英文名）
    assert "全部對應" not in church.get("/members/accounts").text
    assert "沒有可以一次對應" in link_all(church)
    assert members(paths)["陳小明"].line_user_id == ""


def test_duplicate_rows_in_the_member_list_are_never_both_linked(church, paths):
    """有人用 Excel 多貼了一列同名的同工：不能把同一個 LINE 帳號接到兩列上。"""
    items = MemberTable(paths.members_file).load().items
    MemberTable(paths.members_file).save([*items, Member("陳小明", note="Excel 多貼的")])
    line(church, "/我的名字 陳小明", uid("1"))
    link_all(church)
    linked = [m for m in MemberTable(paths.members_file).load().items if m.line_user_id == uid("1")]
    assert len(linked) <= 1


def test_registering_again_replaces_the_earlier_claim(church, paths):
    line(church, "/我的名字 林美", uid("2"))  # 打錯
    line(church, "/我的名字 林美華", uid("2"))  # 再打一次就蓋掉
    assert "已對應 1 位" in link_all(church)
    assert members(paths)["林美華"].line_user_id == uid("2")


def test_ignored_accounts_and_accounts_linked_meanwhile_are_skipped(church, paths):
    line(church, "/我的名字 陳小明", uid("1"))
    line(church, "/我的名字 林美華", uid("2"))
    church.post("/members/accounts/ignore", data={"user_id": uid("1")})
    church.post("/members/accounts/link", data={"user_id": uid("2"), "member_name": "林美華"})  # 先單獨按了
    assert "沒有可以一次對應" in link_all(church)
    assert members(paths)["陳小明"].line_user_id == ""


def test_file_open_in_excel_gives_a_clear_message_and_changes_nothing(church, paths, monkeypatch):
    """Windows 上 Excel 開著 members.csv 會把檔案鎖住，存檔一定失敗。"""
    from pathlib import Path

    line(church, "/我的名字 陳小明", uid("1"))
    before = paths.members_file.read_bytes()
    real_replace = Path.replace

    def locked(self, target):
        if Path(target) == paths.members_file:
            raise PermissionError(13, "The process cannot access the file because it is being used by another process")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", locked)
    monkeypatch.setattr("church_bot.files.SWAP_WAIT", 0)  # 一直被鎖住：重試幾次之後才說清楚
    page = church.post("/members/accounts/link-all")
    assert "Excel" in page.text and "關掉" in page.text
    assert paths.members_file.read_bytes() == before
    assert not list(paths.config_dir.glob("*.tmp"))  # 暫存檔清掉了
    monkeypatch.setattr(Path, "replace", real_replace)
    assert "已對應 1 位" in link_all(church)  # 關掉 Excel 再按一次就好，登記還在


def test_desktop_line_without_user_id_is_told_to_use_the_phone(church):
    body = event_body({"type": "message", "replyToken": "r", "source": {"type": "group", "groupId": gid()},
                       "message": {"type": "text", "text": "/我的名字 陳小明"}})
    church.app.state.webhook.handle(body, sign(body))
    assert "手機" in FakeLine.replies[-1][1]


def test_roll_call_warns_the_admin_when_registration_is_closed(church, paths):
    settings = load_settings(paths)
    settings.chat.collect_names = False
    save_settings(paths, settings)
    text = line(church, "/點名", ADMIN)
    assert "名字登記現在是關的" in text and "開放登記" in text


def test_after_link_all_roll_call_only_lists_who_is_left(church, paths):
    from church_bot.config import Settings
    from tests.conftest import write

    write(paths.config_dir / "roster.csv", "日期,講員,司琴\n2099/1/4,陳小明,林美華\n2099/1/11,阿德哥,美華\n")
    settings = load_settings(paths)
    settings.source.kind, settings.source.csv_path = "csv", "roster.csv"
    save_settings(paths, settings)
    assert isinstance(settings, Settings)
    line(church, "/我的名字 陳小明", uid("1"))
    assert "等管理員確認（1 位）：陳小明" in line(church, "/點名", ADMIN)
    link_all(church)
    after = line(church, "/點名", ADMIN)
    assert "陳小明" not in after.split("✅")[0] and "林美華、阿德哥" in after


def test_single_link_refuses_when_two_rows_share_the_name(church, paths):
    items = MemberTable(paths.members_file).load().items
    MemberTable(paths.members_file).save([*items, Member("林美華", note="Excel 多貼的")])
    line(church, "/我的名字 林美華", uid("2"))
    r = church.post("/members/accounts/link", data={"user_id": uid("2"), "member_name": "林美華"}, follow_redirects=False)
    assert "兩列都叫" in unquote(r.headers["location"])
    assert not [m for m in MemberTable(paths.members_file).load().items if m.line_user_id == uid("2")]


def test_a_brief_lock_is_waited_out(paths, monkeypatch):
    """Windows 上別的執行緒剛好在讀 .env（每個網頁請求都會讀）：換檔被拒絕一下下，等一下就好，不該當成錯誤。"""
    from pathlib import Path

    from church_bot.config import read_env_file

    real_replace = Path.replace
    refused = {"n": 0}

    def briefly_locked(self, target):
        if refused["n"] < 3:
            refused["n"] += 1
            raise PermissionError(5, "Access is denied")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", briefly_locked)
    monkeypatch.setattr("church_bot.files.SWAP_WAIT", 0)
    update_env_file(paths.env_file, {"UI_PASSWORD": "abcd1234"})
    MemberTable(paths.members_file).save([Member("陳小明")])
    assert read_env_file(paths.env_file)["UI_PASSWORD"] == "abcd1234" and refused["n"] == 3
    assert [m.name for m in MemberTable(paths.members_file).load().items] == ["陳小明"]
