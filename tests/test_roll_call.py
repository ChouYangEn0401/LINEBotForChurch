"""點名：服事表上這個群組會提醒到的人，誰 @ 得到、誰登記了在等確認、誰還沒登記。"""

import datetime as dt

import pytest

from church_bot.config import Settings, save_settings
from church_bot.core.accounts import build_accounts
from church_bot.core.directory import Directory
from church_bot.core.roll_call import roll_call
from church_bot.models import Assignment, Member, Roster, ServiceDay, Target, Team
from church_bot.tables import MemberTable, TargetTable
from church_bot.webhook import Command, parse_command
from tests.conftest import TODAY, gid, uid, write
from tests.line_fakes import FakeLine, event_body, say, sign

MEMBERS = [Member("陳小明", ("小明",), line_user_id=uid("1")), Member("林美華", ("美華",)), Member("黃喜樂", ("喜樂",))]
TEAMS = [Team("晨光實體團", ("晨光",), ("小明", "喜樂"))]


def day(date, label="主日崇拜", **roles):
    return ServiceDay(date, tuple(Assignment(k, tuple(v)) for k, v in roles.items()), label=label)


ROSTER = Roster(days=(
    day(TODAY - dt.timedelta(days=7), 講員=["過去的人"]),  # 今天以前的不算
    day(dt.date(2026, 9, 13), 講員=["王牧師"], 司琴=["美華"], 敬拜=["晨光"]),
    day(dt.date(2026, 9, 20), 講員=["王牧師"], 司琴=["小明"], 招待=["阿德哥"]),
    day(dt.date(2026, 9, 19), label="青年崇拜", 司琴=["喜樂"]),
), source="t")


def people(*claims):
    return [{"user_id": uid(str(i)), "display_name": "", "real_name": name, "nicknames": "", "ignored": 0}
            for i, name in enumerate(claims, start=5)]


def test_sorts_everyone_on_the_roster_from_today():
    accounts = build_accounts(people("林美華"), MEMBERS)
    result = roll_call(ROSTER, Directory(MEMBERS, TEAMS), None, accounts, TODAY)
    assert result.linked == ["陳小明"]  # 小團展開成團員；排很多次只算一次
    assert result.pending == ["林美華"]  # 打了「/我的名字 林美華」，等管理員按「對應」
    assert result.missing == ["王牧師", "黃喜樂", "阿德哥"]  # 名單上沒有的人（阿德哥）也要登記
    assert "過去的人" not in result.missing
    assert (result.first, result.last) == (dt.date(2026, 9, 13), dt.date(2026, 9, 19))


def test_only_people_this_group_will_be_reminded_about():
    worship = Target("敬拜團", gid(), roles=("司琴",))
    result = roll_call(ROSTER, Directory(MEMBERS, TEAMS), worship, [], TODAY)
    assert set(result.linked + result.pending + result.missing) == {"林美華", "陳小明", "黃喜樂"}  # 沒有講員、招待
    youth = Target("青年群", gid(), labels=("青年",))
    assert roll_call(ROSTER, Directory(MEMBERS, TEAMS), youth, [], TODAY).missing == ["黃喜樂"]


def test_unknown_roster_name_counts_as_pending_once_someone_claims_it():
    accounts = build_accounts(people("阿德哥"), MEMBERS)
    result = roll_call(ROSTER, Directory(MEMBERS, TEAMS), None, accounts, TODAY)
    assert "阿德哥" in result.pending and "阿德哥" not in result.missing


# ------------------------------------------------------------------ LINE 指令「/點名」

ADMIN = uid("a")


@pytest.fixture
def roll(handler, paths, monkeypatch):
    from zoneinfo import ZoneInfo

    from church_bot.service import BotService

    write(paths.config_dir / "roster.csv", "日期,講員,司琴,招待\n2026/9/13,王牧師,小明,阿德哥\n2026/9/20,王牧師,美華,\n")
    settings = Settings()
    settings.source.kind, settings.source.csv_path = "csv", "roster.csv"
    save_settings(paths, settings)
    MemberTable(paths.members_file).save([Member("管理員", line_user_id=ADMIN, admin=True), *MEMBERS])
    TargetTable(paths.targets_file).save([Target("同工群", gid()), Target("司琴群", gid("d"), roles=("司琴",))])
    monkeypatch.setattr(BotService, "now", lambda self, settings: dt.datetime(2026, 9, 11, 12, 0,
                                                                              tzinfo=ZoneInfo("Asia/Taipei")))
    return handler


def reply(handler, body):
    handler.handle(body, sign(body))
    return FakeLine.replies[-1][1]


def test_parse_roll_call():
    assert parse_command("/點名") == Command("roll_call") and parse_command("／ 點 名") == Command("roll_call")


def test_admin_roll_call_lists_who_still_needs_to_register(roll):
    text = reply(roll, say("/點名", user=ADMIN))
    assert "「同工群」會提醒到的人" in text and "9/13 ～ 9/20" in text
    assert "還沒辦法 @ 到（3 位）：\n王牧師、阿德哥、林美華" in text  # 照服事表的順序，同一個人只列一次
    assert "例如：/我的名字 王牧師" in text
    assert "已經 @ 得到：1 位" in text  # 陳小明有 LINE 帳號
    assert "/我的ID" not in text  # 一步就好：/我的名字 本來就抓得到是誰


def test_roll_call_follows_the_groups_roles_and_pending_claims(roll):
    reply(roll, say("/我的名字 林美華", user=uid("9"), chat=gid("d")))
    text = reply(roll, say("/點名", user=ADMIN, chat=gid("d")))
    assert "「司琴群」" in text and "王牧師" not in text and "阿德哥" not in text
    assert "等管理員確認（1 位）：林美華" in text


def test_roll_call_is_for_admins_in_a_group(roll):
    assert "只有管理員" in reply(roll, say("/點名", user=uid("8")))
    dm = event_body({"type": "message", "replyToken": "r", "source": {"type": "user", "userId": ADMIN},
                     "message": {"type": "text", "text": "/點名"}})
    assert "群組裡打" in reply(roll, dm)


def test_everyone_registered(roll, paths):
    MemberTable(paths.members_file).save([
        Member("管理員", line_user_id=ADMIN, admin=True), Member("陳小明", ("小明",), line_user_id=uid("1")),
        Member("林美華", ("美華",), line_user_id=uid("2")), Member("王牧師", line_user_id=uid("3")),
        Member("阿德哥", line_user_id=uid("4"))])
    assert "都 @ 得到了" in reply(roll, say("/點名", user=ADMIN))
