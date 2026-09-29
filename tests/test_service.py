"""整合測試：CSV 服事表 → 規劃 → 假的 LINE → 紀錄 → 管理員通知。"""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from church_bot.config import Settings, save_settings
from church_bot.errors import ConfigError, MessengerError
from church_bot.messengers.base import Quota
from church_bot.models import DeliveryStatus, Member, Target, Team
from church_bot.service import BotService
from church_bot.tables import MemberTable, TargetTable, TeamTable
from tests.conftest import FakeMessenger, gid, uid, write

ADMIN = uid("f")
ROSTER = """日期,講員,司琴
2026/9/13,王牧師,小明
2026/9/20,李傳道,美華
2026/12/27,王牧師,美華
"""


@pytest.fixture
def fake(monkeypatch) -> FakeMessenger:
    messenger = FakeMessenger()
    monkeypatch.setattr("church_bot.service.build_messenger", lambda settings, paths: messenger)
    return messenger


@pytest.fixture
def service(paths, fake) -> BotService:
    write(paths.config_dir / "roster.csv", ROSTER)
    settings = Settings()
    settings.source.kind = "csv"
    settings.source.csv_path = "config/roster.csv"
    settings.schedule.enabled = False
    settings.line.admin_target_id = ADMIN
    save_settings(paths, settings)
    TargetTable(paths.targets_file).save([Target("同工群", gid()), Target("敬拜團", gid("c"), roles=("司琴",))])
    MemberTable(paths.members_file).save([Member("王大衛牧師", ("王牧師",)), Member("李傳道"), Member("陳小明", ("小明",)),
                                          Member("林美華", ("美華",))])
    svc = BotService(paths)
    svc.now = lambda settings: dt.datetime(2026, 9, 11, 20, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    return svc


def statuses(report):
    return [(d.target_name, d.status) for d in report.deliveries]


def test_run_sends_once_then_skips(service, fake):
    report, _ = service.run("cli")
    assert report.status == "ok", [i.message for i in report.issues]
    assert statuses(report) == [("同工群", DeliveryStatus.SENT), ("敬拜團", DeliveryStatus.SENT)]
    assert "王大衛牧師" in fake.texts_to(gid())[0]
    assert "講員" not in fake.texts_to(gid("c"))[0]  # 敬拜團只收司琴
    assert fake.texts_to(ADMIN) == []  # 一切正常就不吵管理員

    again, _ = service.run("schedule")
    assert [s for _, s in statuses(again)] == [DeliveryStatus.SKIPPED] * 2
    assert len(fake.sent) == 2

    forced, _ = service.run("manual", force=True)
    assert [s for _, s in statuses(forced)] == [DeliveryStatus.SENT] * 2
    assert [r.trigger for r in service.history.recent_runs()] == ["manual", "schedule", "cli"]


def test_scheduled_send_is_tagged_as_automatic(service, fake):
    service.run("cli")
    assert fake.texts_to(gid())[0].startswith("🤖 自動發送\n")


def test_preview_sends_nothing_and_is_not_recorded(service, fake):
    report, plan = service.preview()
    assert [s for _, s in statuses(report)] == [DeliveryStatus.DRY_RUN] * 2
    assert fake.sent == [] and service.history.recent_runs() == []
    assert plan is not None and plan.samples


def test_failure_is_reported_and_admin_is_alerted(service, fake):
    fake.fail[gid()] = MessengerError("LINE 拒絕這個操作", "確認機器人還在群組裡", status_code=403)
    report, _ = service.run("cli")
    assert statuses(report) == [("同工群", DeliveryStatus.FAILED), ("敬拜團", DeliveryStatus.SENT)]
    assert any(i.code == "send_failed" and i.is_error for i in report.issues)
    alert = fake.texts_to(ADMIN)
    assert len(alert) == 1 and "❌" in alert[0] and "同工群" in alert[0]


def test_fatal_error_stops_further_attempts(service, fake):
    fake.fail[gid()] = MessengerError("LINE token 錯誤或已失效", status_code=401)
    report, _ = service.run("cli")
    assert [s for _, s in statuses(report)] == [DeliveryStatus.FAILED, DeliveryStatus.FAILED]
    assert "沒有嘗試" in report.deliveries[1].detail
    assert fake.texts_to(gid("c")) == []


def test_quota_warning(service, fake):
    fake.quota_value = Quota(limit=200, used=195)
    fake.sizes = {gid(): 30, gid("c"): 10}
    report, _ = service.run("cli")
    assert any(i.code == "quota_low" and "40" in i.message for i in report.issues)


def test_missing_token_marks_every_delivery_failed(service, monkeypatch):
    def broken(settings, paths):
        raise ConfigError("還沒設定 LINE Channel access token", "去設定")

    monkeypatch.setattr("church_bot.service.build_messenger", broken)
    report, _ = service.run("cli")
    assert [s for _, s in statuses(report)] == [DeliveryStatus.FAILED] * 2
    assert any(i.code == "messenger_unavailable" for i in report.issues)


def test_source_problem_never_raises(service, paths):
    (paths.config_dir / "roster.csv").unlink()
    report, plan = service.run("cli")
    assert plan is None and report.has_errors
    assert any("找不到服事表檔案" in i.message for i in report.issues)
    assert service.history.last_run().status == "error"


def test_roster_change_resend_policy(service, paths, fake):
    service.run("cli")
    report, _ = service.run("cli")  # 內容沒變 → 不重送
    assert {d.status for d in report.deliveries} == {DeliveryStatus.SKIPPED}

    write(paths.config_dir / "roster.csv", ROSTER.replace("小明", "美華"))
    report, _ = service.run("cli")  # 預設：服事表改過 → 送新的
    assert {d.status for d in report.deliveries} == {DeliveryStatus.SENT}

    settings = Settings.model_validate({"source": {"kind": "csv", "csv_path": "config/roster.csv"},
                                        "schedule": {"enabled": False}, "line": {"admin_target_id": ADMIN},
                                        "behavior": {"resend_if_changed": False}})
    save_settings(paths, settings)
    write(paths.config_dir / "roster.csv", ROSTER.replace("小明", "喜樂"))
    report, _ = service.run("cli")  # 關掉「改了再送」→ 送過就不再送
    assert {d.status for d in report.deliveries} == {DeliveryStatus.SKIPPED}


def test_notify_now_reply_then_scheduled_run_skips_that_target(service):
    messages, note = service.notify_now(gid())
    assert messages and "王大衛牧師" in messages[0].text and messages[0].text.startswith("🙋 手動發送・免費")
    assert "不計入 LINE 額度" in note
    assert [r.trigger for r in service.history.recent_runs()] == ["reply"]

    report, _ = service.run("schedule")
    assert statuses(report) == [("同工群", DeliveryStatus.SKIPPED), ("敬拜團", DeliveryStatus.SENT)]


def test_notify_now_always_replies_because_reply_is_free(service):
    first, _ = service.notify_now(gid())
    again, _ = service.notify_now(gid())
    assert again and [m.text for m in again] == [m.text for m in first]
    report, _ = service.run("schedule")  # 兩次內容一樣，排程還是只略過、不重送
    assert dict(statuses(report))["同工群"] is DeliveryStatus.SKIPPED


def test_failed_reply_is_not_recorded_so_the_schedule_still_sends(service):
    def broken(items):
        raise MessengerError("連不上 LINE", "")

    with pytest.raises(MessengerError):
        service.notify_now(gid(), send=broken)
    assert service.history.recent_runs() == []
    report, _ = service.run("schedule")
    assert dict(statuses(report))["同工群"] is DeliveryStatus.SENT


def test_old_reply_does_not_stop_the_scheduled_push(service, paths):
    """週一有人打 /提醒 看一下，週四的排程還是要照常提醒；2 天內打的才算「已經提醒過」。"""
    import sqlite3

    service.notify_now(gid())
    three_days_ago = (dt.datetime.now().astimezone() - dt.timedelta(days=3)).isoformat(timespec="seconds")
    with sqlite3.connect(paths.db_file) as conn:
        conn.execute("UPDATE deliveries SET created_at=?", (three_days_ago,))
    report, _ = service.run("schedule")
    assert dict(statuses(report))["同工群"] is DeliveryStatus.SENT


def test_notify_now_rejects_chat_without_a_configured_target(service):
    texts, note = service.notify_now(gid("z"))
    assert texts == [] and "不是設定好的提醒群組" in note


def test_health_check_lists_every_area(service):
    names = [item.name for item in service.health()]
    assert names[:5] == ["設定檔", "LINE 群組", "同工名單", "小團", "服事表"] and "管理員通知" in names


def test_personal_user_id_works_as_a_target_for_safe_testing(paths, fake):
    """docs/QUICKSTART_TEST.md 教的做法：LINE 群組的 LINE_ID 填自己的 U... userId，安全地只測試自己。"""
    write(paths.config_dir / "roster.csv", ROSTER)
    settings = Settings()
    settings.source.kind, settings.source.csv_path = "csv", "config/roster.csv"
    settings.schedule.enabled = False
    save_settings(paths, settings)
    me = uid("9")
    TargetTable(paths.targets_file).save([Target("我自己", me, enabled=True)])

    svc = BotService(paths)
    svc.now = lambda settings: dt.datetime(2026, 9, 11, 20, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    report, _ = svc.run("cli")
    assert statuses(report) == [("我自己", DeliveryStatus.SENT)]
    assert fake.texts_to(me) and "王牧師" in fake.texts_to(me)[0]  # 沒設同工名單，名字照服事表原樣顯示


def test_biweekly_schedule_warns_when_lookahead_too_short(paths):
    settings = Settings()
    settings.schedule.every_n_weeks = 2
    settings.behavior.lookahead_days = 7  # 每兩週發一次，但只往後看 7 天 → 中間那週會漏掉
    save_settings(paths, settings)
    issues = BotService(paths).load().issues
    warning = next(i for i in issues if i.code == "lookahead_too_short")
    assert "每 2 週" in warning.message and "14" in warning.hint

    settings.behavior.lookahead_days = 14
    save_settings(paths, settings)
    assert not [i for i in BotService(paths).load().issues if i.code == "lookahead_too_short"]


# ------------------------------------------------------------------ 小團（config/teams.csv）


def test_team_name_on_the_roster_lists_the_whole_team(paths, fake):
    write(paths.config_dir / "roster.csv", """日期,敬拜團,司琴
2026/9/13,晨光實體團,小明
""")
    settings = Settings()
    settings.source.csv_path = "config/roster.csv"
    settings.schedule.enabled = False
    save_settings(paths, settings)
    TargetTable(paths.targets_file).save([Target("同工群", gid())])
    MemberTable(paths.members_file).save([Member("張晨光", ("晨光",)), Member("陳小明", ("小明",)),
                                          Member("林美華", ("美華",))])
    TeamTable(paths.teams_file).save([Team("晨光實體團", ("晨光團",), ("晨光", "小明", "美華"))])

    svc = BotService(paths)
    svc.now = lambda settings: dt.datetime(2026, 9, 11, 20, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    report, plan = svc.run("cli")
    assert "晨光實體團（張晨光、陳小明、林美華）" in fake.texts_to(gid())[0]
    assert not [i for i in report.issues if i.code == "unknown_name"]  # 團名不算「對不到的名字」
    assert "晨光實體團" not in svc.unknown_names()


def test_inactive_team_and_inactive_member_inside_it_are_both_reported(paths, fake):
    write(paths.config_dir / "roster.csv", """日期,敬拜團
2026/9/13,休息小團
""")
    settings = Settings()
    settings.source.csv_path = "config/roster.csv"
    settings.schedule.enabled = False
    save_settings(paths, settings)
    TargetTable(paths.targets_file).save([Target("同工群", gid())])
    MemberTable(paths.members_file).save([Member("周以琳", ("以琳",), active=False), Member("陳小明", ("小明",))])
    TeamTable(paths.teams_file).save([Team("休息小團", (), ("以琳", "小明"), active=False)])

    svc = BotService(paths)
    svc.now = lambda settings: dt.datetime(2026, 9, 11, 20, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    report, _ = svc.run("cli")
    codes = {i.code for i in report.issues}
    assert "inactive_team" in codes and "inactive_member" in codes


# ------------------------------------------------------------------ /別周測試（service.preview_for）


def test_preview_for_another_week_does_not_record_anything(service, fake):
    messages, note = service.preview_for(dt.date(2026, 9, 20), gid())
    assert len(messages) == 1 and "9/20" in messages[0].text
    assert messages[0].text.startswith("🧪 測試預覽")
    assert "沒有發給任何人" in note
    assert fake.sent == []  # 純預覽：不 Push
    assert service.history.recent_runs(10) == []  # 也不寫紀錄，排程時間到了照常送

    report, _ = service.run("schedule")
    assert statuses(report) == [("同工群", DeliveryStatus.SENT), ("敬拜團", DeliveryStatus.SENT)]


def test_preview_for_outside_a_reminder_group_borrows_a_group_and_does_not_tag(service):
    messages, note = service.preview_for(dt.date(2026, 9, 13), uid("z"))
    assert messages and not messages[0].has_mentions
    assert "不是提醒群組" in note and "不會 @ 人" in note


def test_preview_for_a_week_with_no_services_says_so(service):
    messages, note = service.preview_for(dt.date(2026, 10, 4), gid())
    assert messages == [] and "沒有可以提醒的內容" in note
