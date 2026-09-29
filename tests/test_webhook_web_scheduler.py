import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from church_bot.config import ScheduleSettings, Settings, save_settings
from church_bot.models import Member, OutgoingMessage
from church_bot.scheduler import BotScheduler, is_active_week, next_fire_time, previous_fire_time
from church_bot.service import BotService
from church_bot.tables import MemberTable, TargetTable
from church_bot.webhook import Command, SignatureError, parse_command, verify_signature
from tests.conftest import write
from tests.line_fakes import SECRET, FakeLine, event_body, gid, say, sign, uid

# ------------------------------------------------------------------ webhook


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
])
def test_parse_command(text, expected):
    assert parse_command(text) == expected


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
    assert handler.service.history.person(uid())["real_name"] == ""


def test_register_name_overwrites_previous_claim(handler, paths):
    turn_on_name_collection(paths)
    for text in ("/我的名字 林美", "/我的名字 「林美華」"):
        body = say(text)
        handler.handle(body, sign(body))
    assert handler.service.history.person(uid())["real_name"] == "林美華"
    assert "已登記：林美華（LINE 名稱：小美）" in FakeLine.replies[-1][1]

    body = say("/我的名字")
    handler.handle(body, sign(body))
    assert "你登記過的名字：林美華" in FakeLine.replies[-1][1]


def test_register_name_rejects_very_long_names(handler, paths):
    turn_on_name_collection(paths)
    body = say("/我的名字 " + "長" * 21)
    handler.handle(body, sign(body))
    assert "太長" in FakeLine.replies[-1][1] and handler.service.history.person(uid())["real_name"] == ""


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
    person = handler.service.history.person(uid())
    assert person["real_name"] == "陳小明"  # /我的暱稱 不會動到登記的真實姓名
    assert nicknames_of(person) == ("阿明", "小明哥")
    assert "已經登記過" in FakeLine.replies[-1][1]  # 同一個暱稱再打一次不會變兩筆

    for i in range(MAX_NICKNAMES):
        body = say(f"/我的暱稱 綽號{i}")
        handler.handle(body, sign(body))
    assert len(nicknames_of(handler.service.history.person(uid()))) == MAX_NICKNAMES
    assert f"最多登記 {MAX_NICKNAMES} 個" in FakeLine.replies[-1][1]


def test_nickname_needs_collection_to_be_on(handler, paths):
    settings = Settings()
    settings.chat.collect_names = False
    save_settings(paths, settings)
    body = say("/我的暱稱 阿明")
    handler.handle(body, sign(body))
    assert "沒有開放" in FakeLine.replies[-1][1]
    assert handler.service.history.person(uid())["nicknames"] == ""


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

    history = handler.service.history
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
