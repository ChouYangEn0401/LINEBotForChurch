"""Telegram 一次性登入碼（照 CSP 的做法）。"""

import datetime as dt
import re
import time

import pytest

from church_bot.core.login_codes import CODE_TTL, LoginCodeError, LoginCodes


class FakeTelegram:
    def __init__(self):
        self.sent, self.deleted = [], []

    def send(self, chat_id, html):
        self.sent.append((chat_id, html))
        return len(self.sent)

    def delete(self, chat_id, message_id):
        self.deleted.append(message_id)

    def last_code(self):
        return re.search(r"<tg-spoiler>(\d{6})</tg-spoiler>", self.sent[-1][1])[1]


@pytest.fixture
def clock():
    now = [dt.datetime(2026, 10, 7, 20, 0, tzinfo=dt.timezone.utc)]
    return now


def codes(clock):
    return LoginCodes(clock=lambda: clock[0])


def wait_for(cond):
    for _ in range(100):
        if cond():
            return
        time.sleep(0.01)


def test_code_works_once_and_only_from_the_page_that_asked(clock):
    tg, login = FakeTelegram(), codes(clock)
    nonce = login.start(tg, "42")
    code = tg.last_code()
    assert tg.sent[0][0] == "42"
    assert not login.verify("別的頁面", code)  # 攔截到碼也沒用：沒有那一頁的 nonce
    assert login.verify(nonce, code[:3] + " " + code[3:])
    assert not login.verify(nonce, code)  # 用過就失效
    wait_for(lambda: tg.deleted)
    assert tg.deleted == [1]  # Telegram 那則訊息刪掉


def test_three_wrong_tries_void_it_but_strangers_cannot_burn_them(clock):
    tg, login = FakeTelegram(), codes(clock)
    nonce = login.start(tg, "42")
    code = tg.last_code()
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        login.verify("陌生人", wrong)  # 不扣那 3 次
    assert not login.verify(nonce, wrong) and not login.verify(nonce, wrong)
    assert login.verify(nonce, code)
    nonce = login.start(tg, "42")
    code = tg.last_code()
    for _ in range(3):
        login.verify(nonce, wrong)
    assert not login.verify(nonce, code)  # 錯 3 次整筆作廢


def test_expires_and_a_new_request_replaces_the_old_one(clock):
    tg, login = FakeTelegram(), codes(clock)
    old_nonce = login.start(tg, "42")
    old_code = tg.last_code()
    new_nonce = login.start(tg, "42")
    assert not login.verify(old_nonce, old_code)  # 同時只有一筆
    code = tg.last_code()
    clock[0] += CODE_TTL
    assert not login.verify(new_nonce, code)


def test_asking_too_often_is_refused(clock):
    tg, login = FakeTelegram(), codes(clock)
    for _ in range(5):
        login.start(tg, "42")
    with pytest.raises(LoginCodeError, match="太多"):
        login.start(tg, "42")
    clock[0] += dt.timedelta(minutes=11)
    login.start(tg, "42")
