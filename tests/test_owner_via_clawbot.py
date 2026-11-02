"""只給伺服器管理員（擁有者）的通知：先經 ClawBot 的 POST /v1/notify，不行才退回 Notifier_TB（2026-11-02）。

擁有者 2026-11-02：「全部都改，按照現在的分流架構，決定應該要把訊息塞給誰。」——每一則標 stream／kind，
ClawBot 照它的分流表挑 bot；這個專案不保管 Telegram 金鑰。ClawBot 一律用假的（換掉 ``ClawBotLink._post``），
不會真的打這台電腦上的 Host。
"""

from __future__ import annotations

import datetime as dt
import json
import re

import pytest

from church_bot.core import notify
from church_bot.models import Delivery, DeliveryStatus, Issue, RunReport, Severity
from church_bot.service import BotService
from tests.conftest import CHURCH
from tests.test_manager_login import PASSWORD, REMOTE, FakeTelegram, manager  # noqa: F401 - manager 是 fixture
from tests.test_notify_status import dead_port, fake_notifier, write_notifier_config  # noqa: F401 - fixture


def write_clawbot(paths, token: str = "t-owner") -> None:
    folder = notify._clawbot_store(paths).parent
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "gateway.json").write_text(json.dumps({"url": "http://127.0.0.1:8765",
                                                     "tokens": {token: {"user_id": 1, "name": "owner"}}}),
                                         encoding="utf-8")


@pytest.fixture
def clawbot(paths, monkeypatch):
    """假的 ClawBot：記下每一個請求；``reply`` 換掉就能模擬連不上（None）或回錯誤。"""
    state = {"posted": [], "reply": {"ok": True, "bot": "buffer"}}

    def post(self, path, body):
        state["posted"].append((path, body))
        return state["reply"]

    monkeypatch.setattr(notify.ClawBotLink, "_post", post)
    monkeypatch.setattr(notify.ClawBotLink, "is_owner", lambda self: True)
    write_clawbot(paths)
    return state


def test_owner_notices_go_to_clawbot_first_and_say_which_stream(paths, clawbot, fake_notifier):
    write_notifier_config(paths, port=fake_notifier.port)
    assert notify.Notifier(paths).send("✅ 後台已啟動", kind=notify.KIND_HELLO) is True
    assert clawbot["posted"] == [("/v1/notify", {"text": "✅ 後台已啟動", "stream": "system", "kind": "hello",
                                                  "important": False, "silent": False, "source": "LINE"})]
    assert fake_notifier.wait(1, timeout=0.3) == []          # ClawBot 收了就不再經 Notifier_TB


def test_silent_and_important_are_passed_through(paths, clawbot):
    notifier = notify.Notifier(paths)
    notifier.send("彙總", silent=True)
    notifier.send("碼", important=True)
    bodies = [b for _p, b in clawbot["posted"]]
    assert bodies[0]["silent"] is True and bodies[0]["important"] is False
    assert bodies[1]["important"] is True and bodies[1]["stream"] == "system"


def test_a_long_text_is_cut_to_what_clawbot_takes(paths, clawbot):
    notify.Notifier(paths).send("字" * 5000)
    assert len(clawbot["posted"][0][1]["text"]) == notify.CLAWBOT_TEXT_LIMIT


@pytest.mark.parametrize("reply", [None, {"error": "forbidden", "message": "x"}, {"ok": False, "bot": "main"}])
def test_when_clawbot_is_down_or_says_no_it_falls_back_to_notifier_tb(paths, clawbot, fake_notifier, reply):
    clawbot["reply"] = reply
    write_notifier_config(paths, port=fake_notifier.port)
    assert notify.Notifier(paths).send("❌ 後台意外停止", wait=0) is True
    assert len(clawbot["posted"]) == 1
    assert fake_notifier.wait(1) and fake_notifier.payloads == ["❌ 後台意外停止"]


def test_with_neither_it_is_kept_and_resent_through_clawbot_later(paths, clawbot):
    write_notifier_config(paths, port=dead_port())
    clawbot["reply"] = None
    notifier = notify.Notifier(paths)
    assert notifier.send("第一則", wait=0, kind=notify.KIND_HELLO) is False
    assert notifier.pending() == 1
    clawbot["reply"] = {"ok": True, "bot": "buffer"}
    clawbot["posted"].clear()
    assert notifier.send("第二則", wait=0) is True
    texts = [b["text"] for _p, b in clawbot["posted"]]
    assert texts[0] == "第二則" and "補送" in texts[1] and "第一則" in texts[1]
    assert clawbot["posted"][1][1]["kind"] == "hello"        # 補送時也照原本那一流
    assert notifier.pending() == 0


def test_clawbot_alone_is_enough(paths, clawbot):
    """沒有 Notifier_TB、只有 ClawBot 也送得出去；系統檢查那一頁寫的是 ClawBot。"""
    notifier = notify.Notifier(paths)
    assert notifier.enabled and "ClawBot" in notifier.describe()
    assert notifier.send("只有 ClawBot") is True


def test_without_clawbot_nothing_changes(paths, fake_notifier, monkeypatch):
    monkeypatch.setattr(notify.ClawBotLink, "_post", lambda *a: pytest.fail("沒有 ClawBot 不該去打它"))
    write_notifier_config(paths, port=fake_notifier.port)
    assert notify.Notifier(paths).send("舊路") is True
    assert fake_notifier.wait(1) and fake_notifier.payloads == ["舊路"]


# --------------------------------------------------------------------------- 發送彙總響不響


def _report(*statuses: DeliveryStatus, warn: bool = False) -> RunReport:
    report = RunReport(run_id="r", trigger="schedule", dry_run=False, started_at=dt.datetime(2026, 11, 1, 8))
    report.deliveries = [Delivery("群", "g", None, "t", s) for s in statuses]
    if warn:
        report.issues = [Issue(Severity.WARNING, "x", "提醒")]
    return report


def test_a_clean_success_summary_is_quiet_the_rest_ring():
    assert BotService._telegram_silent(_report(DeliveryStatus.SENT, DeliveryStatus.SENT)) is True
    assert BotService._telegram_silent(_report()) is False                                  # 0 則送出
    assert BotService._telegram_silent(_report(DeliveryStatus.SENT, DeliveryStatus.FAILED)) is False
    assert BotService._telegram_silent(_report(DeliveryStatus.SENT, DeliveryStatus.SKIPPED)) is False
    assert BotService._telegram_silent(_report(DeliveryStatus.SENT, warn=True)) is False


# --------------------------------------------------------------------------- 登入碼


def test_login_code_goes_through_clawbot_as_important_without_a_bot_token(client, paths, manager, clawbot):
    from church_bot.config import update_env_file

    update_env_file(paths.env_file, {"TELEGRAM_BOT_TOKEN": "", "SERVER_MANAGER_TELEGRAM_ID": ""})
    page = client.post(CHURCH + "/manager/login", data={"password": PASSWORD}, headers=REMOTE)
    assert "傳 6 位數到我的 Telegram" in page.text            # 有 ClawBot 就算設好了
    page = client.post(CHURCH + "/manager/telegram/send", headers=REMOTE).text
    path, body = clawbot["posted"][-1]
    assert path == "/v1/notify" and body["important"] is True and "<" not in body["text"]
    code = re.search(r"登入碼 (\d{6})", body["text"])[1]
    nonce = re.search(r'name="nonce" value="([^"]+)"', page)[1]
    r = client.post(CHURCH + "/manager/telegram/verify", data={"nonce": nonce, "code": code}, headers=REMOTE,
                    follow_redirects=False)
    assert r.status_code == 303 and FakeTelegram.sent == []


def test_login_code_falls_back_to_the_own_bot_when_clawbot_is_down(client, manager, clawbot):
    clawbot["reply"] = None
    client.post(CHURCH + "/manager/login", data={"password": PASSWORD}, headers=REMOTE)
    client.post(CHURCH + "/manager/telegram/send", headers=REMOTE)
    assert FakeTelegram.sent and FakeTelegram.sent[-1][0] == "42" and "<tg-spoiler>" in FakeTelegram.sent[-1][1]
