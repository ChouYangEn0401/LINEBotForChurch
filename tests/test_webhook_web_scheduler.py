import base64
import datetime as dt
import hashlib
import hmac
import json
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from church_bot.cli import cmd_init
from church_bot.config import ScheduleSettings, Settings, load_settings, save_settings
from church_bot.scheduler import BotScheduler, is_active_week, next_fire_time, previous_fire_time
from church_bot.service import BotService
from church_bot.tables import TargetTable
from church_bot.webhook import SignatureError, WebhookHandler, verify_signature
from tests.conftest import gid, uid, write

SECRET = "s3cret"


def sign(body: bytes, secret: str = SECRET) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


# ------------------------------------------------------------------ webhook


class FakeLine:
    replies: list[tuple[str, str]] = []
    profile_name = "小美"
    profile_lookups = 0

    def __init__(self, *args, **kwargs):
        pass

    def reply(self, token, text):
        FakeLine.replies.append((token, text))

    def group_name(self, group_id):
        return "敬拜團"

    def member_profile(self, chat_id, user_id):
        FakeLine.profile_lookups += 1
        return FakeLine.profile_name

    def send(self, to, message):
        FakeLine.replies.append((to, message.text))

    def close(self):
        pass


@pytest.fixture
def handler(paths, monkeypatch):
    write(paths.env_file, f"LINE_CHANNEL_SECRET={SECRET}\nLINE_CHANNEL_ACCESS_TOKEN=tok\n")
    FakeLine.replies, FakeLine.profile_name, FakeLine.profile_lookups = [], "小美", 0
    monkeypatch.setattr("church_bot.webhook.LineMessenger", FakeLine)
    return WebhookHandler(BotService(paths))


def event_body(*events) -> bytes:
    return json.dumps({"events": list(events)}).encode()


def test_signature():
    assert verify_signature(SECRET, b"{}", sign(b"{}"))
    assert not verify_signature(SECRET, b"{}", sign(b"{}", "other"))


def test_join_adds_disabled_target_and_replies_group_id(handler, paths):
    body = event_body({"type": "join", "replyToken": "r1", "source": {"type": "group", "groupId": gid()}})
    assert handler.handle(body, sign(body)) == 1
    target = TargetTable(paths.targets_file).load().items[0]
    assert (target.name, target.line_id, target.enabled) == ("敬拜團", gid(), False)
    assert FakeLine.replies[0][0] == "r1" and gid() in FakeLine.replies[0][1]


def test_my_id_command_and_normal_chat_is_ignored(handler):
    source = {"type": "group", "groupId": gid(), "userId": uid()}
    body = event_body(
        {"type": "message", "replyToken": "r2", "source": source, "message": {"type": "text", "text": "我的 ID"}},
        {"type": "message", "replyToken": "r3", "source": source, "message": {"type": "text", "text": "大家早安"}},
    )
    handler.handle(body, sign(body))
    assert FakeLine.replies == [("r2", f"你的名字：小美\n你的 LINE ID：\n{uid()}")]


def test_message_from_group_is_remembered_as_a_person(handler, paths):
    from church_bot.service import BotService

    source = {"type": "group", "groupId": gid(), "userId": uid()}
    body = event_body({"type": "message", "replyToken": "r5", "source": source,
                       "message": {"type": "text", "text": "大家早安"}})
    handler.handle(body, sign(body))
    person = BotService(paths).history.person(uid())
    assert person and person["display_name"] == "小美" and person["chat_id"] == gid()


def test_display_name_is_looked_up_at_most_once_a_day(handler):
    source = {"type": "group", "groupId": gid(), "userId": uid()}
    say = lambda token: event_body({"type": "message", "replyToken": token, "source": source,  # noqa: E731
                                    "message": {"type": "text", "text": "早安"}})
    for token in ("a", "b", "c"):
        body = say(token)
        handler.handle(body, sign(body))
    assert FakeLine.profile_lookups == 1

    history = handler.service.history
    stale = (dt.datetime.now().astimezone() - dt.timedelta(days=2)).isoformat(timespec="seconds")
    with history._conn() as conn:
        conn.execute("UPDATE people SET profile_checked_at=? WHERE user_id=?", (stale, uid()))
    FakeLine.profile_name = "美美（改名了）"
    body = say("d")
    handler.handle(body, sign(body))
    assert FakeLine.profile_lookups == 2
    assert history.person(uid())["display_name"] == "美美（改名了）"


def test_member_joined_reports_new_member_names_and_ids(handler):
    body = event_body({
        "type": "memberJoined",
        "replyToken": "r4",
        "source": {"type": "group", "groupId": gid()},
        "joined": {"members": [{"type": "user", "userId": uid()}]},
    })
    handler.handle(body, sign(body))
    assert FakeLine.replies == [("r4", f"歡迎新朋友加入 🙌\n小美（{uid()}）")]


def test_bad_signature_is_rejected(handler):
    with pytest.raises(SignatureError):
        handler.handle(b'{"events":[]}', "wrong")


# ------------------------------------------------------------------ scheduler


def test_previous_fire_time():
    cfg = ScheduleSettings(day_of_week="sat", time="20:00")
    tz = ZoneInfo("Asia/Taipei")
    at = lambda *a: dt.datetime(*a, tzinfo=tz)  # noqa: E731
    assert previous_fire_time(cfg, at(2026, 9, 11, 21, 0)) == at(2026, 9, 5, 20, 0)
    assert previous_fire_time(cfg, at(2026, 9, 12, 20, 30)) == at(2026, 9, 12, 20, 0)
    assert previous_fire_time(cfg, at(2026, 9, 12, 19, 59)) == at(2026, 9, 5, 20, 0)


# 以下日期以 2024-01-01（星期一）為第 0 週往後數：9/12、8/29、9/26 是「偶數週」，9/5、9/19、10/3 是「奇數週」
def test_is_active_week_every_two_weeks():
    cfg = ScheduleSettings(day_of_week="sat", time="20:00", every_n_weeks=2)
    assert is_active_week(cfg, dt.date(2026, 9, 12)) and is_active_week(cfg, dt.date(2026, 8, 29))
    assert not is_active_week(cfg, dt.date(2026, 9, 5)) and not is_active_week(cfg, dt.date(2026, 9, 19))


def test_is_active_week_default_is_always_true():
    cfg = ScheduleSettings()
    assert all(is_active_week(cfg, dt.date(2026, 9, d)) for d in (5, 12, 19, 26))


def test_previous_and_next_fire_time_skip_inactive_weeks():
    cfg = ScheduleSettings(day_of_week="sat", time="20:00", every_n_weeks=2)
    tz = ZoneInfo("Asia/Taipei")
    at = lambda *a: dt.datetime(*a, tzinfo=tz)  # noqa: E731

    # 上一次：週五晚上，最近的週六（9/5）是非發送週 → 應該再往前跳到 8/29
    assert previous_fire_time(cfg, at(2026, 9, 11, 21, 0)) == at(2026, 8, 29, 20, 0)
    # 上一次：發送當天當下就是發送週（9/12）→ 就是今天
    assert previous_fire_time(cfg, at(2026, 9, 12, 20, 30)) == at(2026, 9, 12, 20, 0)
    # 上一次：這週（9/19）是非發送週 → 回到上一個發送週 9/12
    assert previous_fire_time(cfg, at(2026, 9, 19, 20, 30)) == at(2026, 9, 12, 20, 0)

    # 下一次：週五晚上，下週六（9/12）剛好是發送週 → 直接就是它
    assert next_fire_time(cfg, at(2026, 9, 11, 21, 0)) == at(2026, 9, 12, 20, 0)
    # 下一次：今天（9/12）已經發送過了，下週（9/19）是非發送週 → 再往後跳到 9/26
    assert next_fire_time(cfg, at(2026, 9, 12, 20, 30)) == at(2026, 9, 26, 20, 0)
    # 下一次：今天（9/5）還沒到發送時間，但今天是非發送週 → 跳到下個發送週 9/12
    assert next_fire_time(cfg, at(2026, 9, 5, 19, 0)) == at(2026, 9, 12, 20, 0)


def test_run_only_gates_on_active_week_for_the_automatic_schedule_trigger(paths, monkeypatch):
    """「每 N 週」只影響自動排程；補發／手動／指令列一律照常執行，不會被誤判跳過。"""
    settings = Settings()
    settings.schedule.every_n_weeks = 2
    save_settings(paths, settings)

    calls: list[str] = []
    monkeypatch.setattr(BotService, "run", lambda self, trigger, **kw: calls.append(trigger))
    monkeypatch.setattr("church_bot.scheduler.is_active_week", lambda cfg, date: False)
    scheduler = BotScheduler(BotService(paths))

    scheduler._run("schedule")  # 非發送週 → 跳過，不執行
    for trigger in ("catchup", "manual", "cli"):
        scheduler._run(trigger)  # 不受「每 N 週」影響，一定執行

    assert calls == ["catchup", "manual", "cli"]


# ------------------------------------------------------------------ web


@pytest.fixture
def client(paths):
    root = paths.root
    for name in ("settings.example.yaml", "targets.example.csv", "members.example.csv"):
        (root / "config" / name).write_bytes((ROOT / "config" / name).read_bytes())
    (root / ".env.example").write_bytes((ROOT / ".env.example").read_bytes())
    cmd_init(paths, None)
    settings = load_settings(paths)
    settings.messenger.kind = "console"
    settings.schedule.enabled = False
    save_settings(paths, settings)
    from church_bot.web.app import create_app

    with TestClient(create_app(paths)) as c:
        yield c


from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("url", ["/", "/targets", "/members", "/settings", "/runs", "/check", "/help",
                                 "/api/status", "/api/preview", "/healthz"])
def test_pages_render(client, url):
    assert client.get(url).status_code == 200


def test_add_group_then_send_from_web(client, paths):
    r = client.post("/targets/save", data={"name": "同工群", "line_id": gid(), "enabled": "on"}, follow_redirects=False)
    assert r.status_code == 303
    assert "會發送" in client.get("/").text
    r = client.post("/send", data={"force": "0"}, follow_redirects=False)
    assert "已送出" in client.get(r.headers["location"]).text
    assert gid() in (paths.data_dir / "outbox.log").read_text(encoding="utf-8")


def test_invalid_settings_are_not_saved(client, paths):
    before = paths.settings_file.read_text(encoding="utf-8")
    r = client.post("/settings", data={"source_kind": "google_public", "spreadsheet_url": "not a url",
                                       "day_of_week": "sat", "time": "20:00", "timezone": "Asia/Taipei",
                                       "lookahead_days": "7", "roster_low_warning_days": "14"})
    assert r.status_code == 200 and "還沒儲存" in r.text
    assert paths.settings_file.read_text(encoding="utf-8") == before


def test_password_protects_ui_but_not_webhook(client, paths):
    write(paths.env_file, "UI_PASSWORD=pw\n")
    assert client.get("/").status_code == 401  # 沒登入：直接擋下來顯示登入畫面，不是跳轉
    assert client.post("/login", data={"password": "wrong", "next": "/"}).status_code == 401
    r = client.post("/login", data={"password": "pw", "next": "/"}, follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/").status_code == 200  # 登入後 cookie 生效，同一個 client 能繼續逛
    assert client.post("/line/webhook", content=b'{"events":[]}').status_code == 503  # 沒設 secret，但不需要登入


def test_login_rejects_external_redirect_target(client, paths):
    write(paths.env_file, "UI_PASSWORD=pw\n")
    r = client.post("/login", data={"password": "pw", "next": "https://evil.example/phish"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_changing_password_forces_everyone_to_log_in_again(client, paths):
    write(paths.env_file, "UI_PASSWORD=old\n")
    client.post("/login", data={"password": "old", "next": "/"})
    assert client.get("/").status_code == 200
    write(paths.env_file, "UI_PASSWORD=new\n")
    assert client.get("/").status_code == 401  # 舊的 cookie 對不上新密碼，立刻失效
