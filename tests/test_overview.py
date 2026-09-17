import datetime as dt

from church_bot.config import Settings
from church_bot.models import Issue, Member, Roster, ServiceDay, Severity, Target
from church_bot.web.overview import build_steps
from tests.conftest import gid

ROSTER = Roster(days=(ServiceDay(dt.date(2026, 9, 20), ()),), source="Google Sheet：服事表")


def steps(**overrides):
    args = dict(issues=[], settings=Settings(), roster=ROSTER, roster_error="", members=[Member("陳小明")],
                targets=[Target("同工群", gid())], unknown_names=0, pending_claims=0, next_run="2026/09/19 20:00")
    args.update(overrides)
    return {s.title: s for s in build_steps(**args)}


def test_everything_ok_reads_left_to_right():
    result = steps()
    assert list(result) == ["服事表", "同工名單", "LINE 群組", "自動發送"]
    assert {s.status for s in result.values()} == {"ok"}
    assert result["服事表"].summary == "排到 2026/9/20"
    assert result["LINE 群組"].summary == "1 個會收到提醒"


def test_each_step_points_at_its_own_problem():
    result = steps(
        issues=[Issue(Severity.WARNING, "roster_low", "快用完了")],
        members=[], unknown_names=2, pending_claims=1,
        targets=[Target("同工群", gid(), enabled=False)],
    )
    assert result["服事表"].status == "warning"
    assert result["同工名單"].status == "warning" and "2 個名字對不到・1 位 LINE 登記待確認" in result["同工名單"].summary
    assert result["LINE 群組"].status == "error"


def test_unreadable_roster_and_members_not_set_up():
    result = steps(roster=None, roster_error="找不到服事表檔案", members=[])
    assert result["服事表"].status == "error" and result["服事表"].detail == "找不到服事表檔案"
    assert result["同工名單"].status == "off"  # 名單是選用的，沒設定不算錯


def test_schedule_off_and_test_mode():
    settings = Settings()
    settings.schedule.enabled = False
    assert steps(settings=settings)["自動發送"].status == "off"
    settings.messenger.kind = "console"
    assert "測試模式" in steps(settings=settings)["自動發送"].summary


def test_home_page_shows_workflow_and_new_navigation(client):
    page = client.get("/").text
    assert "運作流程" in page and "wf-step" in page
    for label in ("🙋 同工名單", "👥 LINE 群組", "📜 發送紀錄", "❓ 說明"):
        assert label in page
    assert 'href="/check"' not in page.split("<main>")[0]  # 系統檢查收進「說明」，不佔導覽列
