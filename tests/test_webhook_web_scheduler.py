import base64
import datetime as dt
import hashlib
import hmac
import json
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from church_bot.cli import cmd_init
from church_bot.config import ScheduleSettings, load_settings, save_settings
from church_bot.scheduler import previous_fire_time
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

    def __init__(self, *args, **kwargs):
        pass

    def reply(self, token, text):
        FakeLine.replies.append((token, text))

    def group_name(self, group_id):
        return "敬拜團"

    def send(self, to, message):
        FakeLine.replies.append((to, message.text))

    def close(self):
        pass


@pytest.fixture
def handler(paths, monkeypatch):
    write(paths.env_file, f"LINE_CHANNEL_SECRET={SECRET}\nLINE_CHANNEL_ACCESS_TOKEN=tok\n")
    FakeLine.replies = []
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
    assert FakeLine.replies == [("r2", f"你的 LINE ID：\n{uid()}")]


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
    assert client.get("/").status_code == 401
    assert client.get("/", auth=("any", "pw")).status_code == 200
    assert client.post("/line/webhook", content=b'{"events":[]}').status_code == 503  # 沒設 secret，但不需要登入
