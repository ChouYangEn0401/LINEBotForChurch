"""管理網頁的兩層：首頁（所有牧區、還沒分配的群組）和牧區自己的後台（/m/<編號>/…），
以及三種身分：訪客（網站密碼）、牧區管理員（牧區密碼）、伺服器管理員（管理者密碼）。"""

from church_bot.church import add_ministry, edit_ministry, hash_password, load_church
from church_bot.config import update_env_file
from church_bot.models import Target
from church_bot.tables import TargetTable
from tests.conftest import CHURCH, gid


def test_home_lists_every_ministry_and_links_into_it(client, paths):
    add_ministry(paths, "壯年牧區", "約書亞")
    page = client.get(CHURCH + "/").text
    assert "所有牧區" in page and "測試牧區" in page and "壯年牧區" in page and "約書亞" in page
    assert 'href="/m/m2/"' in page and 'data-unit-status="m2"' in page
    inside = client.get(CHURCH + "/m/m2/").text
    assert "← 所有牧區" in inside and "壯年牧區" in inside and 'href="/m/m2/targets"' in inside


def as_visitor(client, paths) -> None:
    """設了管理者密碼、又沒登入管理者：這個瀏覽器就只是訪客（測試的 client 算本機，沒設密碼時是管理者）。"""
    update_env_file(paths.env_file, {"SERVER_MANAGER_PASSWORD": "manager-password-123"})
    client.cookies.clear()


def test_add_ministry_needs_its_password_and_the_creator_gets_in(client, paths):
    as_visitor(client, paths)
    r = client.post(CHURCH + "/ministries/add", data={"name": "兒童牧區", "note": "小羊"}, follow_redirects=False)
    assert "level=error" in r.headers["location"] and len(load_church(paths).ministries) == 1  # 沒設密碼不能建
    r = client.post(CHURCH + "/ministries/add", data={"name": "兒童牧區", "note": "小羊", "password": "lamb"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/m/m2/")
    assert [m.name for m in load_church(paths).ministries] == ["測試牧區", "兒童牧區"]
    assert load_church(paths).get("m2").has_password
    assert client.get(CHURCH + "/m/m2/").status_code == 200  # 建的人這個瀏覽器已經記住了
    r = client.post(CHURCH + "/ministries/add", data={"name": "兒童牧區", "password": "lamb"}, follow_redirects=False)
    assert "level=error" in r.headers["location"]  # 同名不行


def test_unknown_ministry_is_a_friendly_404(client):
    r = client.get(CHURCH + "/m/m99/")
    assert r.status_code == 404 and "找不到牧區" in r.text


def test_unassigned_group_is_assigned_from_the_home_page(client, paths):
    web = client.app.state.web
    web.church.shared.remember_chat(gid("8"), "group", "新的小組")
    assert "還沒分配的群組" in client.get(CHURCH + "/").text and "新的小組" in client.get(CHURCH + "/").text
    assert "新的小組" in client.get("/targets").text  # 牧區的「LINE 群組」頁也看得到，可以直接加
    r = client.post(CHURCH + "/unassigned/assign", data={"chat_id": gid("8"), "ministry_id": "m1"},
                    follow_redirects=False)
    assert r.headers["location"].startswith("/m/m1/targets?edit=")
    assert any(t.line_id == gid("8") and not t.enabled for t in TargetTable(paths.targets_file).load().items)
    assert web.church.unassigned_chats() == []


def test_second_layer_password(client, paths):
    r = client.post("/ministry/password", data={"password": "abcd"}, follow_redirects=False)
    assert r.status_code == 303 and load_church(paths).get("m1").has_password
    assert client.get("/").status_code == 200  # 設密碼的人這個瀏覽器已經記住了

    as_visitor(client, paths)
    locked = client.get("/targets")
    assert "牧區密碼" in locked.text and "/m/m1/unlock" in locked.text and "陳小明" not in locked.text
    assert locked.status_code == 401 and client.get("/api/nav").status_code == 401  # JavaScript 問的回 401
    wrong = client.post(CHURCH + "/m/m1/unlock", data={"password": "nope", "next": "/m/m1/targets"})
    assert "密碼不對" in wrong.text
    r = client.post(CHURCH + "/m/m1/unlock", data={"password": "abcd", "next": "/m/m1/targets"}, follow_redirects=False)
    assert r.headers["location"] == "/m/m1/targets"
    assert "群組" in client.get("/targets").text and "/m/m1/unlock" not in client.get("/targets").text


def test_ministry_without_a_password_is_only_for_the_server_manager(client, paths):
    as_visitor(client, paths)
    page = client.get("/")
    assert page.status_code == 401 and "只有伺服器管理員進得去" in page.text and "/unlock" not in page.text


def test_visitor_and_ministry_admin_cannot_reach_manager_pages(client, paths):
    edit_ministry(paths.church, "m1", password_hash=hash_password("abcd"))
    as_visitor(client, paths)
    home = client.get(CHURCH + "/")
    assert home.status_code == 200 and "訪客" in home.text and "輸入密碼 →" in home.text
    for url in ("/settings", "/history"):
        assert client.get(CHURCH + url).status_code == 403
    assert client.post(CHURCH + "/m/m1/forgot-password").status_code == 403
    client.post(CHURCH + "/m/m1/unlock", data={"password": "abcd"})
    assert "牧區管理員" in client.get("/").text
    assert client.get(CHURCH + "/settings").status_code == 403  # 牧區管理員也不行
    add_ministry(paths, "壯年牧區")
    r = client.post(CHURCH + "/m/m1/ministry/delete", data={"confirm": "測試牧區"})
    assert r.status_code == 403 and len(load_church(paths).ministries) == 2  # 移除牧區只有伺服器管理員


def test_server_manager_is_not_stopped_by_ministry_passwords(client, paths):
    edit_ministry(paths.church, "m1", password_hash=hash_password("abcd"))
    client.cookies.clear()  # 沒設管理者密碼：本機的人就是伺服器管理員（第一次設定用）
    assert client.get("/targets").status_code == 200 and "伺服器管理員" in client.get("/").text
    r = client.post(CHURCH + "/m/m1/forgot-password", follow_redirects=False)
    assert r.headers["location"].startswith("/m/m1/settings") and not load_church(paths).get("m1").has_password


def test_remote_visitor_is_never_the_server_manager(client, paths):
    remote = {"cf-connecting-ip": "1.2.3.4"}  # 從免費模式的臨時網址連進來
    assert client.get(CHURCH + "/settings", headers=remote).status_code == 403


def test_rename_and_remove_a_ministry(client, paths):
    client.post("/ministry/save", data={"name": "青年牧區", "note": "王小明、林美華"})
    assert load_church(paths).get("m1").name == "青年牧區"
    assert "青年牧區" in client.get(CHURCH + "/").text
    r = client.post("/ministry/delete", data={"confirm": "青年牧區"}, follow_redirects=False)
    assert "level=error" in r.headers["location"]  # 最後一個牧區不能移除
    add_ministry(paths, "壯年牧區")
    r = client.post(CHURCH + "/m/m2/ministry/delete", data={"confirm": "打錯"}, follow_redirects=False)
    assert "level=error" in r.headers["location"]
    r = client.post(CHURCH + "/m/m2/ministry/delete", data={"confirm": "壯年牧區"}, follow_redirects=False)
    assert r.headers["location"].startswith("/?") and [m.id for m in load_church(paths).ministries] == ["m1"]


def test_each_ministry_settings_page_only_changes_that_ministry(client, paths):
    from church_bot.config import load_settings

    add_ministry(paths, "壯年牧區")
    form = {"source_kind": "csv", "csv_path": "roster.demo.csv", "day_of_week": "sat", "time": "09:30",
            "timezone": "Asia/Taipei", "lookahead_days": "7", "roster_low_warning_days": "14", "every_n_weeks": "1",
            "messenger_kind": "console", "schedule_enabled": "on", "title": "壯年服事"}
    client.post(CHURCH + "/m/m2/settings", data=form)
    assert load_settings(paths.church.for_ministry("m2")).schedule.time == "09:30"
    assert load_settings(paths).schedule.time == "20:00"
    assert client.app.state.web.scheduler.status_for("m2") == "每星期六 09:30"  # 存檔就重排
