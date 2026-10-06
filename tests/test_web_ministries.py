"""管理網頁的兩層：首頁（所有牧區、還沒分配的群組）和牧區自己的後台（/m/<編號>/…）、第二層密碼。"""

from church_bot.church import add_ministry, load_church
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


def test_add_ministry_from_the_home_page(client, paths):
    r = client.post(CHURCH + "/ministries/add", data={"name": "兒童牧區", "note": "小羊"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/m/m2/")
    assert [m.name for m in load_church(paths).ministries] == ["測試牧區", "兒童牧區"]
    r = client.post(CHURCH + "/ministries/add", data={"name": "兒童牧區"}, follow_redirects=False)
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

    client.cookies.clear()
    locked = client.get("/targets")
    assert "牧區密碼" in locked.text and "/m/m1/unlock" in locked.text and "陳小明" not in locked.text
    assert locked.status_code == 401 and client.get("/api/nav").status_code == 401  # JavaScript 問的回 401
    wrong = client.post(CHURCH + "/m/m1/unlock", data={"password": "nope", "next": "/m/m1/targets"})
    assert "密碼不對" in wrong.text
    r = client.post(CHURCH + "/m/m1/unlock", data={"password": "abcd", "next": "/m/m1/targets"}, follow_redirects=False)
    assert r.headers["location"] == "/m/m1/targets"
    assert "群組" in client.get("/targets").text and "/m/m1/unlock" not in client.get("/targets").text


def test_forgotten_password_can_only_be_cleared_on_this_computer(client, paths):
    client.post("/ministry/password", data={"password": "abcd"})
    client.cookies.clear()
    remote = client.post(CHURCH + "/m/m1/forgot-password", headers={"cf-connecting-ip": "1.2.3.4"},
                         follow_redirects=False)
    assert "level=error" in remote.headers["location"] and load_church(paths).get("m1").has_password
    local = client.post(CHURCH + "/m/m1/forgot-password", follow_redirects=False)
    assert local.headers["location"].startswith("/m/m1/settings") and not load_church(paths).get("m1").has_password


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
