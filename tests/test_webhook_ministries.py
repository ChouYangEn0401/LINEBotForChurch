"""LINE Webhook 依牧區分流：群組在哪個牧區的清單裡，就用那個牧區的設定、名單、資料庫。"""

import re

import pytest

from church_bot.church import add_ministry
from church_bot.config import Settings, load_settings, save_settings
from church_bot.ministries import Church
from church_bot.models import Member, Target
from church_bot.tables import MemberTable, TargetTable
from church_bot.webhook import WebhookHandler
from tests.conftest import write
from tests.line_fakes import SECRET, FakeLine, event_body, gid, say, sign, uid

YOUTH, ADULT, NEW = gid("1"), gid("2"), gid("9")
JULIA, JOSHUA, MEMBER = uid("1"), uid("2"), uid("3")


def dm(text: str, user: str, token: str = "r") -> bytes:
    return event_body({"type": "message", "replyToken": token, "source": {"type": "user", "userId": user},
                       "message": {"type": "text", "text": text}})


@pytest.fixture
def church(root, monkeypatch):
    write(root.env_file, f"LINE_CHANNEL_SECRET={SECRET}\nLINE_CHANNEL_ACCESS_TOKEN=tok\n")
    FakeLine.reset()
    monkeypatch.setattr("church_bot.webhook.LineMessenger", FakeLine)
    for name, group, admin in (("青年牧區", YOUTH, JULIA), ("壯年牧區", ADULT, JOSHUA)):
        m = add_ministry(root, name)
        mpaths = root.for_ministry(m.id)
        settings = Settings()
        settings.chat.collect_names = True
        save_settings(mpaths, settings)
        TargetTable(mpaths.targets_file).save([Target(f"{name}同工群", group)])
        MemberTable(mpaths.members_file).save([Member(f"{name}管理員", line_user_id=admin, admin=True)])
    return Church(root)


@pytest.fixture
def handler(church):
    return WebhookHandler(church)


def send(handler, body):
    handler.handle(body, sign(body))
    return FakeLine.replies[-1][1] if FakeLine.replies else ""


def test_names_registered_in_a_group_go_to_that_ministry_only(handler, church):
    send(handler, say("/我的名字 林恩", user=MEMBER, chat=ADULT))
    assert church.service("m2").history.person(MEMBER)["real_name"] == "林恩"
    assert church.service("m1").history.person(MEMBER) is None
    assert church.shared.person(MEMBER) is None


def test_group_not_in_any_ministry_waits_to_be_assigned(handler, church):
    reply = send(handler, event_body({"type": "join", "replyToken": "j", "source": {"type": "group", "groupId": NEW}}))
    assert "還沒分配的群組" in reply and NEW in reply
    assert [c["chat_id"] for c in church.unassigned_chats()] == [NEW]
    assert "還沒分到任何牧區" in send(handler, say("/提醒", chat=NEW))
    send(handler, say("/我的名字 新朋友", user=MEMBER, chat=NEW))
    assert church.shared.person(MEMBER)["real_name"] == "新朋友"  # 分配時會一起交過去
    church.assign_chat(NEW, "m1")
    assert church.service("m1").history.person(MEMBER)["real_name"] == "新朋友"


def test_remote_config_from_a_group_changes_that_ministry(handler, root):
    reply = send(handler, say("/設定 每幾週=2", user=MEMBER, chat=ADULT))
    assert "壯年牧區" in reply
    to_admin = [text for to, text in FakeLine.replies if to == JOSHUA]  # 驗證碼只給壯年牧區的管理員
    assert len(to_admin) == 1 and not any(to == JULIA for to, _ in FakeLine.replies)
    code = re.search(r"驗證碼：(\d{6})", to_admin[0])[1]
    assert "壯年牧區・每幾週 → 2" in send(handler, say(f"/驗證 {code}", user=MEMBER, chat=ADULT))
    assert load_settings(root.for_ministry("m2")).schedule.every_n_weeks == 2
    assert load_settings(root.for_ministry("m1")).schedule.every_n_weeks == 1


def test_private_message_uses_the_one_ministry_the_admin_runs(handler, root):
    assert "青年牧區" in send(handler, dm("/設定 每幾週=3", JULIA))


def test_admin_of_two_ministries_is_asked_to_use_a_group(handler, root):
    MemberTable(root.for_ministry("m2").members_file).save([Member("王小明", line_user_id=JULIA, admin=True)])
    reply = send(handler, dm("/別周測試 1004", JULIA))
    assert "好幾個牧區" in reply and "青年牧區" in reply and "壯年牧區" in reply


def test_test_week_is_only_for_admins_of_that_ministry(handler):
    assert "只有管理員" in send(handler, say("/別周測試 1004", user=JULIA, chat=ADULT))


def test_bot_removed_from_a_group_alerts_only_that_ministry(handler):
    send(handler, event_body({"type": "leave", "source": {"type": "group", "groupId": YOUTH}}))
    alerts = [(to, text) for to, text in FakeLine.replies if "被移出" in text]
    assert [to for to, _ in alerts] == [JULIA] and "青年牧區" in alerts[0][1]


def test_permissions_name_the_ministry(handler):
    reply = send(handler, say("/權限", chat=YOUTH))
    assert "青年牧區" in reply and "青年牧區管理員" in reply and "壯年牧區管理員" not in reply


# ------------------------------------------------------------------ 每個牧區一個鬧鐘


def test_each_ministry_gets_its_own_alarm(church, root, monkeypatch):
    from church_bot.scheduler import BotScheduler, due_on

    youth = load_settings(root.for_ministry("m1"))
    youth.schedule.enabled, youth.schedule.day_of_week, youth.schedule.time = True, "thu", "20:00"
    save_settings(root.for_ministry("m1"), youth)
    adult = load_settings(root.for_ministry("m2"))
    adult.schedule.enabled, adult.schedule.day_of_week, adult.schedule.time = True, "sat", "09:30"
    save_settings(root.for_ministry("m2"), adult)

    scheduler = BotScheduler(church)
    scheduler._scheduler.start(paused=True)
    try:
        scheduler.reload()
        assert {j.id for j in scheduler._scheduler.get_jobs()} == {"weekly-m1", "weekly-m2"}
        assert scheduler.status_for("m2") == "每星期六 09:30"
        assert scheduler.next_run("m1").weekday() == 3 and scheduler.next_run("m2").weekday() == 5
        assert scheduler.status == "2 個牧區自動發送"

        adult.schedule.enabled = False  # 壯年牧區關掉：重排之後只剩青年牧區
        save_settings(root.for_ministry("m2"), adult)
        scheduler.reload()
        assert [j.id for j in scheduler._scheduler.get_jobs()] == ["weekly-m1"]
        assert scheduler.status_for("m2") == "自動發送已關閉"
    finally:
        scheduler.shutdown()

    import datetime as dt
    thursday = dt.date(2026, 10, 8)
    assert due_on(youth.schedule, thursday) and not due_on(youth.schedule, thursday + dt.timedelta(days=1))
    assert not due_on(adult.schedule, dt.date(2026, 10, 10))  # 關掉的牧區不算到期


def test_alarm_only_sends_its_own_ministry(church, monkeypatch):
    from church_bot.scheduler import BotScheduler
    from church_bot.service import BotService

    sent: list[str] = []
    monkeypatch.setattr(BotService, "run", lambda self, trigger, **kw: sent.append(self.paths.ministry))
    BotScheduler(church)._run("m2", "schedule")
    assert sent == ["m2"]
