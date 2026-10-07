"""伺服器管理員給牧區一組臨時密碼（預設 = 網站密碼）；牧區的人第一次用它進來，一定要換成自己的。"""

import argparse

from church_bot.church import check_password, edit_ministry, hash_password, load_church
from church_bot.cli import cmd_ministries
from church_bot.config import update_env_file
from tests.conftest import CHURCH

SITE = "site-password-123"
MANAGER = "manager-password-123"


def set_up(client, paths):
    update_env_file(paths.env_file, {"UI_PASSWORD": SITE, "SERVER_MANAGER_PASSWORD": MANAGER})
    client.cookies.clear()
    client.post(CHURCH + "/manager/login", data={"password": MANAGER})


def as_julia(client):
    """王小明：只有網站密碼和那一組臨時密碼。"""
    client.cookies.clear()
    client.post(CHURCH + "/login", data={"password": SITE, "next": "/"})


def test_temporary_password_must_be_replaced_on_first_entry(client, paths):
    set_up(client, paths)
    r = client.post(CHURCH + "/m/m1/temp-password", data={"password": ""}, follow_redirects=False)  # 留空 = 網站密碼
    assert "level=ok" in r.headers["location"]
    ministry = load_church(paths.church).get("m1")
    assert ministry.password_temporary and check_password(SITE, ministry.password_hash)
    assert "臨時密碼" in client.get(CHURCH + "/settings").text

    as_julia(client)
    client.post(CHURCH + "/m/m1/unlock", data={"password": SITE, "next": "/m/m1/targets"})
    page = client.get("/targets")
    assert "換成只有你們牧區知道的密碼" in page.text and "群組名稱" not in page.text  # 還不能用這個牧區
    assert client.get("/api/nav").status_code == 403

    for bad, why in ((SITE, "跟現在的（臨時）密碼一樣"), ("abc", "至少 4 個字"), (MANAGER, "管理員的密碼")):
        r = client.post(CHURCH + "/m/m1/set-password", data={"password": bad, "confirm": bad, "next": "/m/m1/targets"})
        assert why in r.text
    assert "不一樣" in client.post(CHURCH + "/m/m1/set-password",
                                   data={"password": "youth-2026", "confirm": "youth-2027"}).text
    r = client.post(CHURCH + "/m/m1/set-password", data={"password": "youth-2026", "confirm": "youth-2026",
                                                         "next": "/m/m1/targets"}, follow_redirects=False)
    assert r.headers["location"].startswith("/m/m1/targets")
    ministry = load_church(paths.church).get("m1")
    assert not ministry.password_temporary and check_password("youth-2026", ministry.password_hash)
    assert client.get("/targets").status_code == 200  # 換好就能用了

    as_julia(client)  # 換過之後，網站密碼（舊的臨時密碼）進不去了
    assert "密碼不對" in client.post(CHURCH + "/m/m1/unlock", data={"password": SITE}).text


def test_server_manager_is_never_forced_to_change(client, paths):
    set_up(client, paths)
    edit_ministry(paths.church, "m1", password_hash=hash_password(SITE), temporary=True)
    assert client.get("/targets").status_code == 200


def test_ministry_admin_cannot_lock_everyone_out_or_reuse_the_site_password(client, paths):
    set_up(client, paths)
    edit_ministry(paths.church, "m1", password_hash=hash_password("youth-2026"))
    as_julia(client)
    client.post(CHURCH + "/m/m1/unlock", data={"password": "youth-2026"})
    r = client.post("/ministry/password", data={"password": "", "clear": "on"}, follow_redirects=False)
    assert "level=error" in r.headers["location"] and load_church(paths.church).get("m1").has_password
    r = client.post("/ministry/password", data={"password": SITE}, follow_redirects=False)
    assert "level=error" in r.headers["location"]
    r = client.post(CHURCH + "/ministries/add", data={"name": "壯年牧區", "password": SITE}, follow_redirects=False)
    assert "level=error" in r.headers["location"]  # 新增牧區也不能用網站密碼當牧區密碼


def test_cli_temp_password_uses_the_site_password(root):
    from church_bot.church import add_ministry

    add_ministry(root, "第一個牧區")
    update_env_file(root.env_file, {"UI_PASSWORD": SITE})
    cmd_ministries(root, argparse.Namespace(action="temp-password", ministry="第一個牧區", password=""))
    ministry = load_church(root).get("m1")
    assert ministry.password_temporary and check_password(SITE, ministry.password_hash)
    assert "password_temporary: true" in root.church_file.read_text(encoding="utf-8")
