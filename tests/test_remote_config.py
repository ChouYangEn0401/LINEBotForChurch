import datetime as dt
import re

import pytest

from church_bot.config import Settings, load_settings, save_settings
from church_bot.core.easter_egg import FLATTERY, PREFIX
from church_bot.remote_config import (
    MAX_CODE_PUSHES_PER_DAY, NothingPending, OPTIONS, Verifier, VerifyError, find_option, parse_assignment,
)
from tests.conftest import CHURCH
from tests.line_fakes import FakeLine, gid, say, sign, uid

COLLECT = find_option("收集名單")


class Clock:
    def __init__(self) -> None:
        self.now = dt.datetime(2026, 9, 17, 20, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += dt.timedelta(**kwargs)


# ------------------------------------------------------------------ parsing


@pytest.mark.parametrize(("arg", "expected"), [
    ("收集名單=開", ("收集名單", "開")),
    ("收集名單 = 開", ("收集名單", "開")),
    ("收集名單：開", ("收集名單", "開")),
    ('"COLLECT_MEMBER":"true"', ("COLLECT_MEMBER", "true")),
    ("「收集名單」 關", ("收集名單", "關")),
    ("收集名單", ("收集名單", "")),
])
def test_parse_assignment(arg, expected):
    assert parse_assignment(arg) == expected


def test_find_option_accepts_aliases_in_any_case():
    assert find_option("COLLECT_MEMBER") is COLLECT
    assert find_option("collect-members") is COLLECT
    assert find_option("Every_N_Weeks").key == "每幾週"
    assert find_option("line_token") is None


def test_option_values():
    assert COLLECT.parse("activate") is True and COLLECT.parse("關") is False
    with pytest.raises(ValueError):
        COLLECT.parse("maybe")
    weeks = find_option("每幾週")
    with pytest.raises(ValueError):
        weeks.parse("9")
    settings = Settings()
    weeks.apply(settings, 2)
    assert settings.schedule.every_n_weeks == 2 and settings.behavior.lookahead_days == 14


def test_secrets_and_admin_can_never_be_changed_from_chat():
    reachable = {k for o in OPTIONS for k in (o.key, *o.aliases)}
    assert not {"line_channel_access_token", "channel_secret", "ui_password", "admin_target_id"} & reachable


# ------------------------------------------------------------------ verifier


def test_correct_code_is_single_use():
    verifier = Verifier()
    pending, code = verifier.start(COLLECT, True, user_id=uid(), chat_id=gid())
    assert re.fullmatch(r"\d{6}", code) and pending.value_text == "開"
    assert code not in repr(pending)  # 驗證碼本身不會被保存
    assert verifier.submit(uid(), gid(), code) is pending
    with pytest.raises(NothingPending):
        verifier.submit(uid(), gid(), code)


def test_only_the_requester_in_the_same_chat_can_verify():
    verifier = Verifier()
    _, code = verifier.start(COLLECT, True, user_id=uid(), chat_id=gid())
    with pytest.raises(NothingPending):
        verifier.submit(uid("c"), gid(), code)
    with pytest.raises(NothingPending):
        verifier.submit(uid(), gid("d"), code)
    assert verifier.current() is not None


def test_three_wrong_codes_cancel_the_request():
    verifier = Verifier()
    _, code = verifier.start(COLLECT, True, user_id=uid(), chat_id=gid())
    wrong = "000000" if code != "000000" else "111111"
    for left in ("2", "1"):
        with pytest.raises(VerifyError, match=left):
            verifier.submit(uid(), gid(), wrong)
    with pytest.raises(VerifyError, match="取消"):
        verifier.submit(uid(), gid(), wrong)
    with pytest.raises(NothingPending):
        verifier.submit(uid(), gid(), code)


def test_code_expires_after_five_minutes():
    clock = Clock()
    verifier = Verifier(clock)
    _, code = verifier.start(COLLECT, True, user_id=uid(), chat_id=gid())
    clock.advance(minutes=5)
    with pytest.raises(NothingPending):
        verifier.submit(uid(), gid(), code)


def test_one_request_at_a_time():
    verifier = Verifier()
    verifier.start(COLLECT, True, user_id=uid(), chat_id=gid())
    with pytest.raises(VerifyError, match="還在等驗證碼"):
        verifier.start(COLLECT, True, user_id=uid(), chat_id=gid())
    with pytest.raises(VerifyError, match="別人"):
        verifier.start(COLLECT, True, user_id=uid("c"), chat_id=gid())
    assert verifier.cancel(uid(), gid())
    verifier.start(COLLECT, True, user_id=uid("c"), chat_id=gid())


def test_line_pushes_are_capped_per_day():
    clock = Clock()
    verifier = Verifier(clock)
    assert all(verifier.allow_push() for _ in range(MAX_CODE_PUSHES_PER_DAY))
    assert not verifier.allow_push()
    clock.advance(days=1, seconds=1)
    assert verifier.allow_push()


# ------------------------------------------------------------------ LINE flow


@pytest.fixture
def line_handler(handler, paths):
    settings = Settings()
    settings.line.admin_target_id = uid("f")
    settings.chat.collect_names = False  # 這組測試用「收集名單 關 → 開」來練習改設定
    save_settings(paths, settings)
    return handler


def test_change_setting_from_line_with_code_sent_to_admin(line_handler, paths, capsys):
    body = say("/設定 收集名單=開")
    line_handler.handle(body, sign(body))
    to_admin = [text for to, text in FakeLine.replies if to == uid("f")]
    assert len(to_admin) == 1 and "收集名單 → 開" in to_admin[0]
    code = re.search(r"驗證碼：(\d{6})", to_admin[0])[1]
    assert code in capsys.readouterr().out  # 也印在執行程式的畫面上
    requester_reply = FakeLine.replies[-1][1]
    assert "需要驗證碼" in requester_reply and code not in requester_reply
    assert not load_settings(paths).chat.collect_names

    body = say(f"/驗證 {code}")
    line_handler.handle(body, sign(body))
    assert FakeLine.replies[-1][1] == "✅ 已更新：測試牧區・收集名單 → 開"
    assert load_settings(paths).chat.collect_names
    assert code not in (paths.data_dir / "church_bot.db").read_bytes().decode("latin-1")


def test_bare_six_digits_count_only_for_the_requester(line_handler, paths):
    body = say("/config COLLECT_MEMBER:true")
    line_handler.handle(body, sign(body))
    code = re.search(r"驗證碼：(\d{6})", next(t for to, t in FakeLine.replies if to == uid("f")))[1]
    replies_before = len(FakeLine.replies)
    body = say(code, user=uid("c"))  # 別人打同樣的數字：當作一般聊天，不回應
    line_handler.handle(body, sign(body))
    assert len(FakeLine.replies) == replies_before
    body = say(code)
    line_handler.handle(body, sign(body))
    assert load_settings(paths).chat.collect_names


def test_remote_config_can_be_turned_off(line_handler, paths):
    settings = load_settings(paths)
    settings.chat.remote_config = False
    save_settings(paths, settings)
    body = say("/設定 收集名單=開")
    line_handler.handle(body, sign(body))
    assert "沒有開放" in FakeLine.replies[-1][1]
    assert line_handler.verifier.current() is None


def test_listing_and_unknown_options_need_no_code(line_handler):
    for text in ("/設定", "/設定 密碼=1234", "/設定 收集名單", "/設定 收集名單=關"):
        body = say(text)
        line_handler.handle(body, sign(body))
    replies = [t for _, t in FakeLine.replies]
    assert "用 LINE 可以改的設定" in replies[0]
    assert "沒有「密碼」這個設定" in replies[1]
    assert "現在是「關」" in replies[2]
    assert "本來就是「關」" in replies[3]
    assert line_handler.verifier.current() is None


# ------------------------------------------------------------------ web approve / reject


def test_web_shows_pending_change_and_can_approve(client, paths):
    verifier = client.app.state.webhook.verifier
    verifier.start(COLLECT, True, user_id=uid(), chat_id=gid(), requester="小美", chat_label="敬拜團",
                   ministry_id="m1", ministry_name="測試牧區")
    page = client.get(CHURCH + "/").text  # 首頁和每個牧區都看得到
    assert "小美（測試牧區・敬拜團）要把「收集名單」" in page
    client.post(CHURCH + "/remote-config/approve")
    assert load_settings(paths).chat.collect_names and verifier.current() is None


def test_web_reject(client, paths):
    settings = load_settings(paths)
    settings.chat.collect_names = False
    save_settings(paths, settings)
    verifier = client.app.state.webhook.verifier
    verifier.start(COLLECT, True, user_id=uid(), chat_id=gid(), ministry_id="m1")
    client.post(CHURCH + "/remote-config/reject")
    assert verifier.current() is None and not load_settings(paths).chat.collect_names


def test_settings_page_saves_chat_switches(client, paths):
    form = {"source_kind": "csv", "csv_path": "config/roster.demo.csv", "day_of_week": "sat", "time": "20:00",
            "timezone": "Asia/Taipei", "lookahead_days": "7", "roster_low_warning_days": "14", "every_n_weeks": "1",
            "messenger_kind": "console", "collect_names": "on"}
    client.post("/settings", data=form)
    chat = load_settings(paths).chat
    assert chat.collect_names and not chat.remote_config and not chat.send_code_to_admin
    assert not chat.easter_egg  # 沒勾就是關（彩蛋預設不開）


# ------------------------------------------------------------------ 彩蛋（見 core/easter_egg.py）


def test_easter_egg_is_off_by_default(line_handler):
    body = say("/設定 收集名單=關")  # 本來就是「關」
    line_handler.handle(body, sign(body))
    assert "本來就是「關」" in FakeLine.replies[-1][1]


def test_easter_egg_says_yes_instead_of_correcting(line_handler, paths):
    settings = load_settings(paths)
    settings.chat.easter_egg = True
    save_settings(paths, settings)

    for _ in range(len(FLATTERY) + 1):
        body = say("/設定 收集名單=關")  # 每次都是「本來就是這樣」
        line_handler.handle(body, sign(body))

    replies = [t for _, t in FakeLine.replies][-(len(FLATTERY) + 1):]
    assert all(r.startswith(PREFIX + "\n") for r in replies)
    # 三句輪完回到第一句
    assert [r.split("\n", 1)[1] for r in replies] == [*FLATTERY, FLATTERY[0]]
    assert "不用改" not in "".join(replies)
    assert not load_settings(paths).chat.collect_names  # 設定沒有被動到
    assert line_handler.verifier.current() is None  # 也沒有啟動驗證碼流程


def test_easter_egg_can_be_switched_on_from_line(line_handler, paths):
    """「/設定 彩蛋=開」跟其他設定一樣：要驗證碼，改完真的生效。"""
    body = say("/設定 彩蛋=開")
    line_handler.handle(body, sign(body))
    code = re.search(r"驗證碼：(\d{6})", next(t for to, t in FakeLine.replies if to == uid("f")))[1]
    body = say(f"/驗證 {code}")
    line_handler.handle(body, sign(body))
    assert FakeLine.replies[-1][1] == "✅ 已更新：測試牧區・彩蛋 → 開"
    assert load_settings(paths).chat.easter_egg

    # 開起來之後，「本來就是這樣」的那種修改就會變成順著說「好」——連「彩蛋」自己也不例外
    body = say("/設定 彩蛋模式=開")
    line_handler.handle(body, sign(body))
    assert FakeLine.replies[-1][1].startswith(PREFIX + "\n")


def test_easter_egg_is_listed_in_the_options(line_handler):
    body = say("/設定")
    line_handler.handle(body, sign(body))
    listing = FakeLine.replies[-1][1]
    assert f"共 {len(OPTIONS)} 項" in listing and "・彩蛋：現在是「關」" in listing


def test_easter_egg_does_not_touch_real_changes(line_handler, paths):
    settings = load_settings(paths)
    settings.chat.easter_egg = True
    save_settings(paths, settings)
    body = say("/設定 收集名單=開")  # 真的要改：照舊要驗證碼
    line_handler.handle(body, sign(body))
    assert "需要驗證碼" in FakeLine.replies[-1][1]
    assert line_handler.verifier.current() is not None
    assert not load_settings(paths).chat.collect_names
