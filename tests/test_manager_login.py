"""伺服器管理員登入：本機只要密碼；從外面要密碼 + 驗證器 App 或 Telegram 登入碼（照 CSP）。"""

import re

import pytest

from church_bot.church import edit_ministry, hash_password
from church_bot.config import update_env_file
from church_bot.core import totp
from tests.conftest import CHURCH

REMOTE = {"cf-connecting-ip": "1.2.3.4"}  # 從免費模式的臨時網址連進來
PASSWORD = "manager-password-123"


class FakeTelegram:
    sent: list[tuple[str, str]] = []

    def __init__(self, token):
        self.token = token

    def send(self, chat_id, html):
        FakeTelegram.sent.append((chat_id, html))
        return 1

    def delete(self, chat_id, message_id):
        pass


@pytest.fixture
def manager(client, paths, monkeypatch):
    secret = totp.new_secret()
    update_env_file(paths.env_file, {"SERVER_MANAGER_PASSWORD": PASSWORD, "SERVER_MANAGER_TOTP_SECRET": secret,
                                     "TELEGRAM_BOT_TOKEN": "bot-token", "SERVER_MANAGER_TELEGRAM_ID": "42"})
    edit_ministry(paths.church, "m1", password_hash=hash_password("abcd"))
    FakeTelegram.sent = []
    monkeypatch.setattr("church_bot.web.manager.TelegramSender", FakeTelegram)
    client.cookies.clear()
    return secret


def is_manager(client, headers=None):
    return client.get(CHURCH + "/settings", headers=headers or {}).status_code == 200


def test_on_this_computer_the_password_is_enough(client, manager):
    assert not is_manager(client)
    assert client.post(CHURCH + "/manager/login", data={"password": "nope"}).status_code == 401
    r = client.post(CHURCH + "/manager/login", data={"password": PASSWORD, "next": "/m/m1/"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/m/m1/")
    assert is_manager(client) and client.get("/targets").status_code == 200  # 牧區密碼對管理者無效
    client.post(CHURCH + "/manager/logout")
    assert not is_manager(client)


def test_from_outside_the_password_alone_is_not_enough(client, manager):
    page = client.post(CHURCH + "/manager/login", data={"password": PASSWORD}, headers=REMOTE)
    assert page.status_code == 200 and "驗證器 App 的 6 位數" in page.text and "傳 6 位數到我的 Telegram" in page.text
    assert not is_manager(client, REMOTE)
    code = totp.code_at(manager, totp.now_counter())
    r = client.post(CHURCH + "/manager/totp", data={"code": code, "next": "/"}, headers=REMOTE, follow_redirects=False)
    assert r.status_code == 303 and is_manager(client, REMOTE)


def test_second_step_needs_the_first(client, manager):
    code = totp.code_at(manager, totp.now_counter())
    r = client.post(CHURCH + "/manager/totp", data={"code": code}, headers=REMOTE)
    assert r.status_code == 401 and "重新輸入管理者密碼" in r.text and not is_manager(client, REMOTE)


def test_three_wrong_codes_start_over(client, manager):
    client.post(CHURCH + "/manager/login", data={"password": PASSWORD}, headers=REMOTE)
    for _ in range(2):
        assert "不對" in client.post(CHURCH + "/manager/totp", data={"code": "000000"}, headers=REMOTE).text
    assert "重新輸入管理者密碼" in client.post(CHURCH + "/manager/totp", data={"code": "000000"}, headers=REMOTE).text
    code = totp.code_at(manager, totp.now_counter())
    client.post(CHURCH + "/manager/totp", data={"code": code}, headers=REMOTE)
    assert not is_manager(client, REMOTE)


def test_telegram_code_only_works_on_the_page_that_asked(client, manager):
    client.post(CHURCH + "/manager/login", data={"password": PASSWORD}, headers=REMOTE)
    page = client.post(CHURCH + "/manager/telegram/send", headers=REMOTE).text
    assert FakeTelegram.sent[-1][0] == "42"
    code = re.search(r"<tg-spoiler>(\d{6})</tg-spoiler>", FakeTelegram.sent[-1][1])[1]
    nonce = re.search(r'name="nonce" value="([^"]+)"', page)[1]
    assert "不對" in client.post(CHURCH + "/manager/telegram/verify", data={"nonce": "別的頁面", "code": code},
                                 headers=REMOTE).text
    r = client.post(CHURCH + "/manager/telegram/verify", data={"nonce": nonce, "code": code}, headers=REMOTE,
                    follow_redirects=False)
    assert r.status_code == 303 and is_manager(client, REMOTE)


def test_outside_login_is_refused_when_no_second_step_is_set_up(client, paths, manager):
    update_env_file(paths.env_file, {"SERVER_MANAGER_TOTP_SECRET": "", "TELEGRAM_BOT_TOKEN": ""})
    r = client.post(CHURCH + "/manager/login", data={"password": PASSWORD}, headers=REMOTE)
    assert r.status_code == 403 and "要先設定驗證器 App 或 Telegram" in r.text


def test_too_many_outside_failures_pause_outside_logins_but_not_this_computer(client, manager):
    for _ in range(5):
        client.post(CHURCH + "/manager/login", data={"password": "wrong"}, headers=REMOTE)
    r = client.post(CHURCH + "/manager/login", data={"password": PASSWORD}, headers=REMOTE)
    assert r.status_code == 429
    client.post(CHURCH + "/manager/login", data={"password": PASSWORD})
    assert is_manager(client)


def test_changing_the_manager_password_logs_everyone_out(client, paths, manager):
    client.post(CHURCH + "/manager/login", data={"password": PASSWORD})
    assert is_manager(client)
    update_env_file(paths.env_file, {"SERVER_MANAGER_PASSWORD": "another-password-456"})
    assert not is_manager(client)


def test_manager_gets_past_the_site_password_too(client, paths, manager):
    update_env_file(paths.env_file, {"UI_PASSWORD": "site"})
    assert client.get(CHURCH + "/").status_code == 401
    assert "伺服器管理員登入" in client.get(CHURCH + "/").text
    client.post(CHURCH + "/manager/login", data={"password": PASSWORD})
    assert client.get(CHURCH + "/").status_code == 200 and is_manager(client)


# ------------------------------------------------------------------ 全教會設定 → 伺服器管理員


def env(paths):
    from church_bot.config import read_env_file

    return read_env_file(paths.env_file)


def test_set_the_manager_password_on_this_computer(client, paths):
    client.cookies.clear()  # 還沒設管理者密碼：本機就是管理者
    page = client.get(CHURCH + "/settings").text
    assert "伺服器管理員" in page and "還沒設" in page
    r = client.post(CHURCH + "/settings/manager-password", data={"password": "short", "confirm": "short"},
                    follow_redirects=False)
    assert "level=error" in r.headers["location"]
    r = client.post(CHURCH + "/settings/manager-password", data={"password": PASSWORD, "confirm": PASSWORD},
                    follow_redirects=False)
    assert "level=ok" in r.headers["location"] and env(paths)["SERVER_MANAGER_PASSWORD"] == PASSWORD
    assert is_manager(client)  # 改的人自己不用重新登入


def test_authenticator_is_saved_only_after_a_matching_code(client, paths):
    client.cookies.clear()
    page = client.get(CHURCH + "/settings?totp_setup=1").text
    import re as _re

    secret = _re.search(r'name="secret" value="([A-Z2-7]+)"', page)[1]
    assert "<svg" in page or "輸入設定金鑰" in page
    r = client.post(CHURCH + "/settings/totp/confirm", data={"secret": secret, "code": "000000"}, follow_redirects=False)
    assert "level=error" in r.headers["location"] and not env(paths).get("SERVER_MANAGER_TOTP_SECRET")
    code = totp.code_at(secret, totp.now_counter())
    client.post(CHURCH + "/settings/totp/confirm", data={"secret": secret, "code": code})
    assert env(paths)["SERVER_MANAGER_TOTP_SECRET"] == secret


def test_manager_keys_cannot_be_changed_from_outside(client, paths, manager):
    client.post(CHURCH + "/manager/login", data={"password": PASSWORD}, headers=REMOTE)
    client.post(CHURCH + "/manager/totp", data={"code": totp.code_at(manager, totp.now_counter())}, headers=REMOTE)
    assert is_manager(client, REMOTE)
    assert "只能在執行機器人的那台電腦上改" in client.get(CHURCH + "/settings", headers=REMOTE).text
    for url, data in (("/settings/manager-password", {"password": "x" * 12, "confirm": "x" * 12}),
                      ("/settings/totp/remove", {}), ("/settings/telegram", {"chat_id": "1"})):
        r = client.post(CHURCH + url, data=data, headers=REMOTE, follow_redirects=False)
        assert "level=error" in r.headers["location"]
    assert env(paths)["SERVER_MANAGER_PASSWORD"] == PASSWORD and env(paths)["SERVER_MANAGER_TOTP_SECRET"] == manager


def test_telegram_settings_and_test_message(client, paths, manager, monkeypatch):
    monkeypatch.setattr("church_bot.web.app.TelegramSender", FakeTelegram)
    client.post(CHURCH + "/manager/login", data={"password": PASSWORD})
    r = client.post(CHURCH + "/settings/telegram", data={"chat_id": "not a number"}, follow_redirects=False)
    assert "level=error" in r.headers["location"]
    client.post(CHURCH + "/settings/telegram", data={"token": "", "chat_id": "777"})  # token 留空 = 不改
    assert env(paths)["SERVER_MANAGER_TELEGRAM_ID"] == "777" and env(paths)["TELEGRAM_BOT_TOKEN"] == "bot-token"
    client.post(CHURCH + "/settings/telegram/test")
    assert FakeTelegram.sent[-1][0] == "777"
