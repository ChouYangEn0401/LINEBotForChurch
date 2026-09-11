import datetime as dt

from church_bot.config import BehaviorSettings, MessageSettings
from church_bot.core.directory import Directory
from church_bot.core.planner import Planner
from church_bot.core.renderer import Renderer
from church_bot.models import Assignment, Member, Roster, ServiceDay, Target
from tests.conftest import TODAY, gid

MEMBERS = [Member("陳小明", ("小明",)), Member("林美華", ("美華",)), Member("周以琳", ("以琳",), active=False)]
TARGET = Target("同工群", gid())


def day(date, label="", **roles):
    return ServiceDay(date, tuple(Assignment(k, tuple(v)) for k, v in roles.items()), label=label)


def plan(*days, targets=(TARGET,), members=MEMBERS, **behavior):
    planner = Planner(Renderer(MessageSettings()), Directory(members), BehaviorSettings(**behavior))
    return planner.plan(Roster(days=tuple(days), source="t"), list(targets), TODAY)


def codes(p):
    return {i.code: i for i in p.issues}


D13, D20, D27 = dt.date(2026, 9, 13), dt.date(2026, 9, 20), dt.date(2026, 9, 27)
FAR = dt.date(2026, 12, 27)


def test_picks_days_inside_window():
    p = plan(day(D13, 司琴=["小明"]), day(D20, 司琴=["美華"]), day(FAR, 司琴=["美華"]))
    assert [d.date for d in p.days] == [D13]
    assert len(p.messages) == 1 and "陳小明" in p.messages[0].message.text
    assert p.samples and not any(i.is_error for i in p.issues)


def test_gap_in_roster_is_an_error():
    assert codes(plan(day(D27, 司琴=["小明"])))["roster_gap"].is_error


def test_expired_roster_is_an_error():
    assert codes(plan(day(dt.date(2026, 9, 6), 司琴=["小明"])))["roster_expired"].is_error


def test_roster_running_low_is_a_warning():
    issue = codes(plan(day(D13, 司琴=["小明"]), day(D20, 司琴=["美華"])))["roster_low"]
    assert issue.severity.value == "warning" and "9/20" in issue.message


def test_unknown_name_warning_has_suggestion():
    issue = codes(plan(day(D13, 司琴=["小明明"]), day(FAR)))["unknown_name"]
    assert "小明明" in issue.message and "陳小明" in issue.hint


def test_inactive_member_warning():
    assert "周以琳" in codes(plan(day(D13, 司琴=["以琳"]), day(FAR)))["inactive_member"].message


def test_target_with_nothing_to_send_is_reported():
    target = Target("招待組", gid("c"), roles=("招待",))
    p = plan(day(D13, 司琴=["小明"]), day(FAR), targets=[target])
    assert not p.messages and "招待組" in codes(p)["target_nothing"].message


def test_label_filter_and_disabled_target():
    youth = Target("青年", gid("c"), labels=("青年",))
    off = Target("停用群", gid("d"), enabled=False)
    p = plan(day(D13, "主日崇拜", 司琴=["小明"]), day(FAR), targets=[youth, off])
    assert not p.messages and "target_nothing" in codes(p)


def test_empty_day_is_a_warning():
    assert "day_empty" in codes(plan(day(D13, 司琴=[], 音控=[]), day(FAR)))


def test_no_members_is_only_info():
    p = plan(day(D13, 司琴=["小明"]), day(FAR), members=[])
    assert codes(p)["no_members"].severity.value == "info" and "unknown_name" not in codes(p)
