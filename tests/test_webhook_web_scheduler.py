import datetime as dt
import time
from zoneinfo import ZoneInfo

import pytest

from church_bot import __version__
from church_bot.config import ScheduleSettings, Settings, load_settings, save_settings
from church_bot.core.public_url import public_base
from church_bot.models import Member, OutgoingMessage
from church_bot.scheduler import (
    QUOTA_EVERY_MINUTES, QUOTA_JOB_ID, BotScheduler, is_active_week, next_fire_time, previous_fire_time,
)
from church_bot.ministries import Church
from church_bot.service import BotService
from church_bot.tables import MemberTable, TargetTable
from church_bot.webhook import Command, SignatureError, parse_command, verify_signature
from tests.conftest import CHURCH, REPO_ROOT, write
from tests.line_fakes import SECRET, FakeLine, event_body, gid, say, sign, uid

def test_quota_is_refreshed_when_the_management_web_app_starts(paths, monkeypatch):
    """用量不會停在舊數字：開管理網頁先問一次，之後每 5 分鐘回來看該不該再問（見 core/quota.py）。"""
    calls: list[dict] = []
    monkeypatch.setattr(Church, "refresh_quota", lambda self, **kw: calls.append(kw))
    monkeypatch.setattr(BotScheduler, "_maybe_catch_up", lambda self: None)
    settings = Settings()
    settings.schedule.enabled = False
    save_settings(paths, settings)

    scheduler = BotScheduler(Church(paths.church))
    scheduler.start()
    try:
        deadline = time.monotonic() + 5
        while not calls and time.monotonic() < deadline:
            time.sleep(0.02)
        assert calls == [{}]
        job = scheduler._scheduler.get_job(QUOTA_JOB_ID)
        assert job is not None and job.trigger.interval == dt.timedelta(minutes=QUOTA_EVERY_MINUTES)
    finally:
        scheduler.shutdown()


def test_quota_refresh_failure_never_breaks_the_scheduler(paths, monkeypatch):
    def boom(self, **kw):
        raise RuntimeError("LINE 爛掉了")

    monkeypatch.setattr(Church, "refresh_quota", boom)
    BotScheduler(Church(paths.church))._refresh_quota()  # 不丟例外，只寫進記錄檔


# ------------------------------------------------------------------ webhook


def test_signature():
    assert verify_signature(SECRET, b"{}", sign(b"{}"))
    assert not verify_signature(SECRET, b"{}", sign(b"{}", "other"))


def test_join_adds_disabled_target_and_replies_group_id(handler, paths):
    """教會只有一個牧區：機器人被邀進新群組，直接加進那個牧區（跟以前單一牧區時一樣）。"""
    body = event_body({"type": "join", "replyToken": "r1", "source": {"type": "group", "groupId": gid("j")}})
    assert handler.handle(body, sign(body)) == 1
    target = TargetTable(paths.targets_file).load().items[-1]
    assert (target.name, target.line_id, target.enabled) == ("敬拜團", gid("j"), False)
    assert FakeLine.replies[0][0] == "r1" and gid("j") in FakeLine.replies[0][1] and "測試牧區" in FakeLine.replies[0][1]


def test_my_id_command_and_normal_chat_is_ignored(handler):
    source = {"type": "group", "groupId": gid(), "userId": uid()}
    body = event_body(
        {"type": "message", "replyToken": "r2", "source": source, "message": {"type": "text", "text": "/我的 ID"}},
        {"type": "message", "replyToken": "r3", "source": source, "message": {"type": "text", "text": "大家早安"}},
        {"type": "message", "replyToken": "r4", "source": source, "message": {"type": "text", "text": "我的ID"}},
    )
    handler.handle(body, sign(body))
    assert FakeLine.replies == [("r2", f"你的名字：小美\n你的 LINE ID：\n{uid()}")]


@pytest.mark.parametrize(("text", "expected"), [
    ("/群組ID", Command("chat_id")),
    ("／群組 id", Command("chat_id")),  # 全形斜線、空白、大小寫都沒關係
    ("/ID", Command("chat_id")),
    ("/我的ID", Command("my_id")),
    ("/說明", Command("help")),
    ("群組ID", None),  # 沒有「/」就當作一般聊天
    ("id", None),
    ("/今天吃什麼", None),  # 不認得的指令不回應
    ("/群組ID 多打的字", None),
    ("", None),
    ("/我的名字 王小明", Command("my_name", "王小明")),
    ("/我的名字王小明", Command("my_name", "王小明")),
    ("/我的名字=王小明", Command("my_name", "王小明")),
    ("/我的名字", Command("my_name", "")),
    ("/name Amy Chen", Command("my_name", "Amy Chen")),
    ("/names Amy", None),
    ("/現在提醒", Command("notify_now")),
    ("/立即提醒", Command("notify_now")),
    ("/help", Command("help")),  # /說明、/help、/? 都是同一個指令
    ("/?", Command("help")),
    ("/？", Command("help")),  # 全形問號
    ("/我的暱稱 阿明", Command("my_nickname", "阿明")),
    ("/暱稱=阿明", Command("my_nickname", "阿明")),
    ("/權限", Command("permissions")),
    ("/我的權限", Command("my_permissions")),
    ("/別周測試 1004", Command("test_week", "1004")),
    ("/別週測試 10/04", Command("test_week", "10/04")),
    ("/別周測試", Command("test_week", "")),
    ("/服務網址", Command("service_url")),
    ("/網址", Command("service_url")),
    ("/url", Command("service_url")),
])
def test_parse_command(text, expected):
    assert parse_command(text) == expected


# ------------------------------------------------------------------ 版本號


def test_help_shows_the_version(handler):
    """「你們跑的是哪一版？」打 /說明 就看得到，不用請人去開管理網頁。"""
    body = say("/說明")
    handler.handle(body, sign(body))
    assert f"版本 v{__version__}" in FakeLine.replies[-1][1]


def test_new_friend_greeting_also_shows_the_version(handler):
    body = event_body({"type": "follow", "replyToken": "r",
                       "source": {"type": "user", "userId": uid()}})
    handler.handle(body, sign(body))
    assert f"版本 v{__version__}" in FakeLine.replies[-1][1]


def test_version_lives_in_exactly_one_place():
    """pyproject 的版本是從 church_bot.__version__ 算出來的；誰把它改回寫死的數字就會紅燈。"""
    read_configuration = pytest.importorskip("setuptools.config.pyprojecttoml").read_configuration
    resolved = read_configuration(REPO_ROOT / "pyproject.toml")["project"]
    assert resolved["version"] == __version__


def turn_on_name_collection(paths):
    settings = Settings()
    settings.chat.collect_names = True
    save_settings(paths, settings)


def test_register_name_is_refused_while_collection_is_off(handler, paths):
    settings = Settings()
    settings.chat.collect_names = False
    save_settings(paths, settings)
    body = say("/我的名字 林美華")
    handler.handle(body, sign(body))
    assert "沒有開放" in FakeLine.replies[-1][1]
    assert handler.church.service("m1").history.person(uid())["real_name"] == ""


def test_register_name_overwrites_previous_claim(handler, paths):
    turn_on_name_collection(paths)
    for text in ("/我的名字 林美", "/我的名字 「林美華」"):
        body = say(text)
        handler.handle(body, sign(body))
    assert handler.church.service("m1").history.person(uid())["real_name"] == "林美華"
    assert "已登記：林美華（LINE 名稱：小美）" in FakeLine.replies[-1][1]

    body = say("/我的名字")
    handler.handle(body, sign(body))
    assert "你登記過的名字：林美華" in FakeLine.replies[-1][1]


def test_register_name_rejects_very_long_names(handler, paths):
    turn_on_name_collection(paths)
    body = say("/我的名字 " + "長" * 21)
    handler.handle(body, sign(body))
    assert "太長" in FakeLine.replies[-1][1] and handler.church.service("m1").history.person(uid())["real_name"] == ""


def test_remind_command_works_for_anyone(handler, monkeypatch):
    """指令本身呼叫 service.notify_now（另外在 test_service.py 測），這裡只測指令這一層：不是管理員也能用。"""
    def fake_notify(self, chat_id, send=None):
        messages, note = [OutgoingMessage(text="本週提醒內容")], "✅ 已免費送出"
        send([*messages, note])
        return messages, note

    monkeypatch.setattr(BotService, "notify_now", fake_notify)
    for text in ("/提醒", "/現在提醒"):
        body = say(text)  # 預設的 uid() 不是管理員
        handler.handle(body, sign(body))
        assert FakeLine.replies[-2:] == [("r", "本週提醒內容"), ("r", "✅ 已免費送出")]


def test_remind_in_a_chat_that_is_not_a_target_explains_why(handler):
    body = say("/提醒")
    handler.handle(body, sign(body))
    assert "不是設定好的提醒群組" in FakeLine.replies[-1][1]


def test_notify_now_with_nothing_to_send_only_replies_the_reason(handler, paths, monkeypatch):
    settings = Settings()
    settings.line.admin_target_id = uid("f")
    save_settings(paths, settings)
    monkeypatch.setattr(BotService, "notify_now", lambda self, chat_id, send=None: ([], "這週已經送過了"))

    body = say("/現在提醒", user=uid("f"))
    handler.handle(body, sign(body))
    assert FakeLine.replies[-1] == ("r", "這週已經送過了")


# ------------------------------------------------------------------ 暱稱（一個人可以有很多個稱呼）


def test_nicknames_add_up_instead_of_replacing_each_other(handler, paths):
    from church_bot.core.history import MAX_NICKNAMES, nicknames_of

    turn_on_name_collection(paths)
    for text in ("/我的名字 陳小明", "/我的暱稱 阿明", "/我的暱稱 小明哥", "/我的暱稱 阿明"):
        body = say(text)
        handler.handle(body, sign(body))
    person = handler.church.service("m1").history.person(uid())
    assert person["real_name"] == "陳小明"  # /我的暱稱 不會動到登記的真實姓名
    assert nicknames_of(person) == ("阿明", "小明哥")
    assert "已經登記過" in FakeLine.replies[-1][1]  # 同一個暱稱再打一次不會變兩筆

    for i in range(MAX_NICKNAMES):
        body = say(f"/我的暱稱 綽號{i}")
        handler.handle(body, sign(body))
    assert len(nicknames_of(handler.church.service("m1").history.person(uid()))) == MAX_NICKNAMES
    assert f"最多登記 {MAX_NICKNAMES} 個" in FakeLine.replies[-1][1]


def test_nickname_needs_collection_to_be_on(handler, paths):
    settings = Settings()
    settings.chat.collect_names = False
    save_settings(paths, settings)
    body = say("/我的暱稱 阿明")
    handler.handle(body, sign(body))
    assert "沒有開放" in FakeLine.replies[-1][1]
    assert handler.church.service("m1").history.person(uid())["nicknames"] == ""


def test_nickname_without_an_argument_lists_what_was_registered(handler, paths):
    turn_on_name_collection(paths)
    for text in ("/我的暱稱 阿明", "/我的暱稱"):
        body = say(text)
        handler.handle(body, sign(body))
    assert "你登記過的暱稱：阿明" in FakeLine.replies[-1][1]


# ------------------------------------------------------------------ /權限、/我的權限


def test_permissions_command_names_the_admin_and_who_may_do_what(handler, paths):
    settings = Settings()
    settings.line.admin_target_id = uid("f")
    save_settings(paths, settings)
    MemberTable(paths.members_file).save([Member("王大衛牧師", line_user_id=uid("f"))])

    body = say("/權限", user=uid())
    handler.handle(body, sign(body))
    text = FakeLine.replies[-1][1]
    assert "管理員：王大衛牧師" in text and uid("f") not in text  # 只露頭尾，不把整個 ID 貼在群組裡
    assert "/提醒" in text and "只有管理員：/別周測試" in text and "要驗證碼才算數：/設定" in text


def test_permissions_command_says_when_no_admin_is_set(handler):
    body = say("/權限")
    handler.handle(body, sign(body))
    assert "管理員：還沒設定" in FakeLine.replies[-1][1]


def test_my_permissions_shows_whether_the_person_can_be_tagged(handler, paths):
    settings = Settings()
    settings.line.admin_target_id = uid("f")
    save_settings(paths, settings)
    MemberTable(paths.members_file).save([Member("陳小明", ("小明",), line_user_id=uid())])

    body = say("/我的權限", user=uid())
    handler.handle(body, sign(body))
    mine = FakeLine.replies[-1][1]
    assert "同工名單：陳小明（其他寫法：小明）" in mine and "提醒會 @ 到你" in mine
    assert "身分：一般成員" in mine

    body = say("/我的資料", user=uid("f"), token="r2")
    handler.handle(body, sign(body))
    admin_view = FakeLine.replies[-1][1]
    assert "還沒對應到你" in admin_view and "管理員 👑" in admin_view and "/別周測試" in admin_view


# ------------------------------------------------------------------ /別周測試（只有管理員）


def test_test_week_is_admin_only(handler, paths):
    settings = Settings()
    settings.line.admin_target_id = uid("f")
    save_settings(paths, settings)
    body = say("/別周測試 1004", user=uid())  # 一般成員
    handler.handle(body, sign(body))
    assert "只有管理員可以用" in FakeLine.replies[-1][1]


def test_test_week_previews_the_given_week_for_the_admin(handler, paths, monkeypatch):
    settings = Settings()
    settings.line.admin_target_id = uid("f")
    save_settings(paths, settings)
    seen: list = []

    def fake_preview(self, day, chat_id=""):
        seen.append((day, chat_id))
        return [OutgoingMessage(text="10/4 那週的內容")], "🧪 試印結果"

    monkeypatch.setattr(BotService, "preview_for", fake_preview)
    body = say("/別周測試 1004", user=uid("f"))
    handler.handle(body, sign(body))
    (day, chat_id), = seen
    assert (day.month, day.day, chat_id) == (10, 4, gid())  # 年份由「離今天最近」決定
    assert FakeLine.replies[-2:] == [("r", "10/4 那週的內容"), ("r", "🧪 試印結果")]


def test_test_week_explains_bad_and_missing_dates(handler, paths):
    settings = Settings()
    settings.line.admin_target_id = uid("f")
    save_settings(paths, settings)
    for text in ("/別周測試", "/別周測試 哪一天"):
        body = say(text, user=uid("f"))
        handler.handle(body, sign(body))
    assert "用法：/別周測試 1004" in FakeLine.replies[-2][1]
    assert "看不懂日期「哪一天」" in FakeLine.replies[-1][1]


def test_message_from_group_is_remembered_as_a_person(handler, paths):
    from church_bot.service import BotService

    source = {"type": "group", "groupId": gid(), "userId": uid()}
    body = event_body({"type": "message", "replyToken": "r5", "source": source,
                       "message": {"type": "text", "text": "大家早安"}})
    handler.handle(body, sign(body))
    person = BotService(paths).history.person(uid())
    assert person and person["display_name"] == "小美" and person["chat_id"] == gid()


def test_display_name_is_looked_up_at_most_once_a_day(handler):
    for token in ("a", "b", "c"):
        body = say("早安", token)
        handler.handle(body, sign(body))
    assert FakeLine.profile_lookups == 1

    history = handler.church.service("m1").history
    stale = (dt.datetime.now().astimezone() - dt.timedelta(days=2)).isoformat(timespec="seconds")
    with history._conn() as conn:
        conn.execute("UPDATE people SET profile_checked_at=? WHERE user_id=?", (stale, uid()))
    FakeLine.profile_name = "美美（改名了）"
    body = say("早安", "d")
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


# ------------------------------------------------------------------ 對外網址（core/public_url.py）


def test_service_url_command_replies_the_current_address(handler, paths):
    """換了網址不用貼給每位管理員：誰想進管理網頁，自己打「/服務網址」問機器人。"""
    write(paths.env_file, f"LINE_CHANNEL_SECRET={SECRET}\nLINE_CHANNEL_ACCESS_TOKEN=tok\nUI_PASSWORD=pw\n")
    body = say("/服務網址")
    handler.handle(body, sign(body), "https://abc.trycloudflare.com")
    reply = FakeLine.replies[-1][1]
    assert "https://abc.trycloudflare.com" in reply and "密碼" in reply

    body = say("/網址", token="r2")  # 重開免費模式 → 新網址，再問就是新的
    handler.handle(body, sign(body), "https://xyz.trycloudflare.com")
    assert "https://xyz.trycloudflare.com" in FakeLine.replies[-1][1]


def test_service_url_is_only_given_to_admins_while_there_is_no_password(handler, paths):
    """沒設密碼時，拿到網址就等於能改設定：那種狀態下只回給管理員，並要求先設密碼。"""
    body = say("/服務網址")
    handler.handle(body, sign(body), "https://abc.trycloudflare.com")
    reply = FakeLine.replies[-1][1]
    assert "https://abc" not in reply and "還沒設密碼" in reply

    settings = load_settings(paths)
    settings.line.admin_target_id = uid()  # 打指令的人就是管理員
    save_settings(paths, settings)
    body = say("/服務網址", token="r2")
    handler.handle(body, sign(body), "https://abc.trycloudflare.com")
    assert "https://abc.trycloudflare.com" in FakeLine.replies[-1][1] and "⚠️" in FakeLine.replies[-1][1]


def test_service_url_says_so_when_it_does_not_know_the_address_yet(handler):
    body = say("/服務網址")
    handler.handle(body, sign(body))
    assert "還不知道對外的網址" in FakeLine.replies[-1][1]


def test_public_base_keeps_real_hosts_and_drops_local_ones():
    assert public_base("abc.trycloudflare.com", "https") == "https://abc.trycloudflare.com"
    assert public_base("abc.trycloudflare.com, proxy.internal", "https, http") == "https://abc.trycloudflare.com"
    assert public_base("192.168.0.5:8787", "http") == "http://192.168.0.5:8787"
    assert public_base("127.0.0.1:8787", "http") == "" and public_base("localhost", "http") == ""
    assert public_base("", "https") == ""


def test_the_url_line_connected_to_is_remembered_only_after_the_signature_checks_out(handler):
    body = say("早安")
    handler.handle(body, sign(body), "https://abc.trycloudflare.com")
    current = handler.church.service("m1").service_url()
    assert current.url == "https://abc.trycloudflare.com" and current.source == "webhook"
    assert current.describe().endswith("LINE 剛剛連到這個網址")

    with pytest.raises(SignatureError):  # 偽造的 Host 不會被記下來（簽章先驗過才記）
        handler.handle(body, sign(body, "other"), "https://evil.example")
    assert handler.church.service("m1").service_url().url == "https://abc.trycloudflare.com"


def test_webhook_route_passes_the_forwarded_host_through(client, paths, monkeypatch):
    """cloudflared 會把原本的網址放在 X-Forwarded-Host；那就是外面看到的網址。"""
    seen: list[str] = []
    monkeypatch.setattr("church_bot.webhook.WebhookHandler.handle",
                        lambda self, body, signature, public_url="": seen.append(public_url) or 1)
    client.post(CHURCH + "/line/webhook", content=b"{}",
                headers={"x-line-signature": "x", "x-forwarded-host": "abc.trycloudflare.com",
                         "x-forwarded-proto": "https"})
    # 沒有經過 cloudflared（直接連本機）：那不是別人連得到的網址，不記
    client.post(CHURCH + "/line/webhook", content=b"{}", headers={"x-line-signature": "x", "host": "127.0.0.1:8787"})
    assert seen == ["https://abc.trycloudflare.com", ""]


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
    scheduler = BotScheduler(Church(paths.church))

    scheduler._run("m1", "schedule")  # 非發送週 → 跳過，不執行
    for trigger in ("catchup", "manual", "cli"):
        scheduler._run("m1", trigger)  # 不受「每 N 週」影響，一定執行

    assert calls == ["catchup", "manual", "cli"]


# ------------------------------------------------------------------ web


@pytest.mark.parametrize("url", ["/", "/roster", "/targets", "/targets?new=1", "/targets?edit=敬拜團", "/members",
                                 "/members?new=1", "/members?edit=陳小明", "/members/accounts", "/members/teams",
                                 "/members/teams?new=1", "/settings", "/runs", "/check", "/help",
                                 "/api/status", "/api/preview", "/api/roster/sheet", "/api/nav",
                                 CHURCH + "/", CHURCH + "/?new=1", CHURCH + "/settings", CHURCH + "/help",
                                 CHURCH + "/api/status", CHURCH + "/healthz"])
def test_pages_render(client, url):
    assert client.get(url).status_code == 200


def test_admin_mode_reveals_the_admin_pages_in_the_menu(client):
    """右上角「切換身分」只是把進階頁面收起來（cookie），不是權限：頁面本身照樣打得開。"""
    assert client.get("/settings").status_code == 200
    r = client.post(CHURCH + "/admin-mode", data={"enabled": "1", "next": "/m/m1/settings#schedule"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/m/m1/settings?msg=")
    page = client.get("/").text
    sidebar = page.split('<section id="view"')[0]
    assert '<body class="admin"' in page
    assert 'href="/m/m1/settings"' in sidebar and 'href="/m/m1/check"' in sidebar
    assert 'href="/settings"' in client.get(CHURCH + "/").text.split('<section id="view"')[0]  # 全教會設定

    r = client.post(CHURCH + "/admin-mode", data={"enabled": "0", "next": "https://evil.example/"},
                    follow_redirects=False)
    assert r.headers["location"].startswith("/?")  # 外部網址不理它，回首頁
    assert 'href="/m/m1/settings"' not in client.get("/").text.split('<section id="view"')[0]


def test_sidebar_status_says_how_each_page_is_doing(client):
    """側欄每一項底下那行小字是「現在的狀況」，有事要處理的標數字（app.js 來問這一支）。"""
    nav = client.get("/api/nav").json()
    assert set(nav) >= {"index", "roster", "members", "targets", "runs", "settings"}
    assert nav["roster"]["sub"].startswith("排到 ") and nav["roster"]["tone"] == "ok"
    assert nav["members"]["sub"].startswith("13 位")
    assert nav["targets"] == {"sub": "0 個會收到提醒", "count": 0, "tone": "bad"}  # 範例群組都還沒啟用
    assert nav["index"]["tone"] == "bad" and nav["index"]["count"] >= 1
    assert nav["runs"]["sub"] == "還沒有發送過"


def test_quota_tile_comes_from_the_snapshot_and_api_keeps_it_fresh(client, paths, monkeypatch):
    """主控台的用量那一格：先畫存下來的數字，再由 app.js 問 /api/quota 換成最新的（⟳ = force=1）。"""
    from church_bot.messengers.base import Quota
    from tests.conftest import FakeMessenger

    assert "data-quota" not in client.get("/").text  # 測試模式（console）不顯示這一格
    assert client.get(CHURCH + "/api/quota").json() == {}

    settings = load_settings(paths)
    settings.messenger.kind = "line"
    save_settings(paths, settings)
    fake = FakeMessenger()
    fake.quota_value = Quota(limit=200, used=37)
    monkeypatch.setattr("church_bot.service.build_messenger", lambda settings, paths: fake)

    assert client.get(CHURCH + "/api/quota").json()["value"] == "37 / 200"
    page = client.get("/").text
    assert "data-quota" in page and "37 / 200" in page and "剛剛更新" in page  # 存下來了，不用再連網

    fake.quota_value = Quota(limit=200, used=190)
    assert client.get(CHURCH + "/api/quota").json()["value"] == "37 / 200"  # 快照還很新，不重問
    assert "37 / 200" in client.get(CHURCH + "/").text  # 首頁也是同一份（整個 LINE 帳號一份）
    forced = client.get(CHURCH + "/api/quota?force=1").json()
    assert forced["value"] == "190 / 200" and forced["low"] and "還剩 10 則" in forced["detail"]


def test_roster_page_shows_the_sheet_and_what_the_program_read(client):
    page = client.get("/roster").text
    assert "整張表" in page and "程式讀到的結果" in page and "換一份服事表" in page and "roster.js" in page
    data = client.get("/api/roster/sheet").json()
    sheet = data["sheets"][0]
    assert data["ok"] and sheet["layout"] == "wide" and sheet["header_row"] == 0 and sheet["date_axis"] == "row"
    assert sheet["dates"]["1"].startswith("20") and len(sheet["dates"]) >= 4 and sheet["cols"]["date"] == 0  # 第 2 列起是日期
    assert sheet["rows"][0][0] == "日期" and isinstance(data["unknown_names"], list)


def test_roster_source_keeps_the_old_sheet_when_the_new_one_cannot_be_read(client, paths, monkeypatch):
    from church_bot.errors import SourceError
    from church_bot.service import BotService

    before = paths.settings_file.read_text(encoding="utf-8")
    r = client.post("/roster/source", data={"url": "not a url"}, follow_redirects=False)
    assert r.status_code == 303 and "level=error" in r.headers["location"]

    def unreadable(self, settings, today, use_cache=False):
        raise SourceError("這份 Google Sheet 沒有開放", "去按共用")

    monkeypatch.setattr(BotService, "fetch_roster", unreadable)
    url = "https://docs.google.com/spreadsheets/d/1234567890abcdefghijklmnop/edit#gid=0"
    r = client.post("/roster/source", data={"url": url}, follow_redirects=False)
    assert "level=error" in r.headers["location"]
    assert paths.settings_file.read_text(encoding="utf-8") == before  # 讀不到就不換


def test_roster_source_switches_to_google_after_a_successful_trial_read(client, paths, monkeypatch):
    from church_bot.models import Roster, ServiceDay
    from church_bot.service import BotService

    monkeypatch.setattr(BotService, "fetch_roster", lambda self, settings, today, use_cache=False:
                        Roster(days=(ServiceDay(dt.date(2026, 10, 4), ()),), source="Google Sheet（公開連結）：gid=0"))
    url = "https://docs.google.com/spreadsheets/d/1234567890abcdefghijklmnop/edit#gid=0"
    r = client.post("/roster/source", data={"url": url}, follow_redirects=False)
    assert "level=ok" in r.headers["location"]
    source = load_settings(paths).source
    assert source.kind == "google_public" and source.spreadsheet_url == url and source.worksheet == ""


def test_add_and_edit_forms_open_in_a_drawer_over_the_list(client):
    """新增／編輯開在右邊的抽屜，清單留在後面；沒有在新增或編輯時，頁面上沒有表單。"""
    assert "/members/save" not in client.get("/members").text and "data-drawer" not in client.get("/members").text
    editing = client.get("/members?edit=陳小明").text
    assert "data-drawer" in editing and "/members/save" in editing and 'value="陳小明"' in editing
    assert 'id="member-table"' in editing  # 清單還在
    assert "/targets/save" not in client.get("/targets").text and "/targets/save" in client.get("/targets?new=1").text
    assert "/teams/save" not in client.get("/members/teams").text
    assert "/teams/save" in client.get("/members/teams?new=1").text


def test_add_group_then_send_from_web(client, paths):
    r = client.post("/targets/save", data={"name": "同工群", "line_id": gid(), "enabled": "on"}, follow_redirects=False)
    assert r.status_code == 303
    assert "會發送" in client.get("/").text
    r = client.post("/send", data={"force": "0"}, follow_redirects=False)
    assert "已送出" in client.get(CHURCH + r.headers["location"]).text
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
    assert client.get(CHURCH + "/").status_code == 401  # 首頁也一樣
    assert client.post(CHURCH + "/login", data={"password": "wrong", "next": "/"}).status_code == 401
    r = client.post(CHURCH + "/login", data={"password": "pw", "next": "/m/m1/"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/m/m1/"
    assert client.get("/").status_code == 200  # 登入後 cookie 生效，同一個 client 能繼續逛
    assert client.get(CHURCH + "/").status_code == 200
    assert client.post(CHURCH + "/line/webhook", content=b'{"events":[]}').status_code == 503  # 沒設 secret，但不需要登入


def test_login_rejects_external_redirect_target(client, paths):
    write(paths.env_file, "UI_PASSWORD=pw\n")
    r = client.post(CHURCH + "/login", data={"password": "pw", "next": "https://evil.example/phish"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_changing_password_forces_everyone_to_log_in_again(client, paths):
    write(paths.env_file, "UI_PASSWORD=old\n")
    client.post(CHURCH + "/login", data={"password": "old", "next": "/"})
    assert client.get("/").status_code == 200
    write(paths.env_file, "UI_PASSWORD=new\n")
    assert client.get("/").status_code == 401  # 舊的 cookie 對不上新密碼，立刻失效


def test_stale_process_gets_a_restart_hint_instead_of_a_scary_error(client, monkeypatch):
    """畫面每次重新讀檔、程式是開機時載入的：更新完沒重開 = 新畫面配舊程式（畫面要的變數程式還沒給）。"""
    import jinja2
    from fastapi.testclient import TestClient

    from church_bot.web import ministry as web_ministry

    def missing_variable(*args, **kwargs):
        raise jinja2.UndefinedError("'editing_team' is undefined")

    monkeypatch.setattr(web_ministry.MemberTable, "load", missing_variable)
    quiet = TestClient(client.app, raise_server_exceptions=False, base_url="http://testserver/m/m1/")  # 看畫面就好
    body = quiet.get("/members").text
    assert "這個視窗還在跑舊的版本" in body and "2-start" in body


# ------------------------------------------------------------------ 管理員：同工名單裡勾一下就好


def test_member_flagged_as_admin_can_use_admin_commands(handler, paths, monkeypatch):
    MemberTable(paths.members_file).save([Member("陳小明", line_user_id=uid(), admin=True), Member("林美華")])
    monkeypatch.setattr(BotService, "preview_for", lambda self, day, chat_id="": ([OutgoingMessage(text="試印")], "🧪"))
    for text in ("/別周測試 1004", "/權限", "/我的權限"):
        body = say(text, user=uid())
        handler.handle(body, sign(body))
    assert FakeLine.replies[0][1] == "試印"
    assert "管理員：陳小明" in FakeLine.replies[2][1]
    assert "管理員 👑" in FakeLine.replies[3][1]


def test_admin_toggle_on_the_members_page(client, paths):
    r = client.post("/members/admin", data={"name": "陳小明", "enabled": "1", "next": "/members"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/m/m1/members?")
    assert next(m for m in MemberTable(paths.members_file).load().items if m.name == "陳小明").admin
    assert 'value="0"' in client.get("/members").text  # 開著的那一列，按鈕變成「關」
    client.post("/members/admin", data={"name": "陳小明", "enabled": "0"})
    assert not next(m for m in MemberTable(paths.members_file).load().items if m.name == "陳小明").admin


def test_alerts_go_to_admin_members_when_no_target_is_set(paths, monkeypatch):
    from tests.conftest import FakeMessenger, write

    fake = FakeMessenger()
    monkeypatch.setattr("church_bot.service.build_messenger", lambda settings, paths: fake)
    write(paths.config_dir / "roster.csv", "日期,講員\n2026/9/13,王牧師\n")
    settings = Settings()
    settings.source.kind, settings.source.csv_path, settings.schedule.enabled = "csv", "roster.csv", False
    save_settings(paths, settings)
    MemberTable(paths.members_file).save([Member("陳小明", line_user_id=uid("1"), admin=True),
                                          Member("林美華", line_user_id=uid("2"), admin=True), Member("沒帳號", admin=True)])
    service = BotService(paths)
    service.now = lambda settings: dt.datetime(2026, 9, 11, 20, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    assert service.admin_targets(settings) == [uid("1"), uid("2")]
    service.run("cli")  # 沒有群組 → 有錯誤 → 通知每一位管理員
    assert fake.texts_to(uid("1")) and fake.texts_to(uid("2"))
