"""點名：服事表上這個群組會提醒到的人，誰 @ 得到、誰登記了在等確認、誰還沒登記。"""

import datetime as dt

from church_bot.core.accounts import build_accounts
from church_bot.core.directory import Directory
from church_bot.core.roll_call import roll_call
from church_bot.models import Assignment, Member, Roster, ServiceDay, Target, Team
from tests.conftest import TODAY, gid, uid

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
