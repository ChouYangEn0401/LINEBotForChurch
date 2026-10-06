"""變更紀錄：每次存檔留一筆、相同內容只存一份、直接用 Excel 改的也抓得到、可以還原。"""

from church_bot.church import edit_ministry, hash_password
from church_bot.config import load_settings, save_settings
from church_bot.core import versions
from church_bot.ministries import Church
from church_bot.models import Member, Target
from church_bot.tables import MemberTable, TargetTable
from tests.conftest import CHURCH, gid


def test_every_save_is_recorded_with_its_source(paths):
    church = Church(paths.church)
    table = TargetTable(paths.targets_file)
    with versions.source("管理網頁"):
        table.save([Target("同工群", gid())])
        table.save([Target("同工群", gid()), Target("敬拜團", gid("c"))])
        table.save([Target("同工群", gid()), Target("敬拜團", gid("c"))])  # 內容一樣：不多記
    changes = church.versions.changes("m1")
    assert [(c.label, c.source, c.summary) for c in changes] == [
        ("LINE 群組", "管理網頁", "新增「敬拜團」"), ("LINE 群組", "管理網頁", "建立")]
    diff = church.versions.diff(changes[0])
    assert any(kind == "add" and "敬拜團" in line for kind, line in diff)


def test_settings_summary_names_what_changed(paths):
    church = Church(paths.church)
    settings = load_settings(paths)
    save_settings(paths, settings)
    settings.schedule.time = "21:00"
    save_settings(paths, settings)
    assert church.versions.changes("m1")[0].summary == "什麼時候發 › time：20:00 → 21:00"


def test_external_edit_is_noticed_and_can_be_restored(paths):
    church = Church(paths.church)
    MemberTable(paths.members_file).save([Member("王小明")])
    original = paths.members_file.read_bytes()
    paths.members_file.write_text("名字\n被 Excel 改壞了\n", encoding="utf-8-sig")  # 不經過程式
    MemberTable(paths.members_file).save([Member("林美華")])
    sources = [c.source for c in church.versions.changes("m1")]
    assert sources[:2] == ["程式", versions.EXTERNAL]

    excel = church.versions.changes("m1")[1]  # 「直接改檔案」那一筆：還原成改之前 = 程式存的那一版
    assert church.versions.content(excel.before) == original


def test_untracked_files_and_other_roots_are_ignored(paths, tmp_path):
    church = Church(paths.church)
    (paths.config_dir / "roster.csv").write_text("x", encoding="utf-8")
    from church_bot.files import write_text

    write_text(paths.config_dir / "roster.csv", "日期\n")  # 服事表 CSV 不記
    assert church.versions.changes("m1") == []


def test_password_hash_never_shows_in_history(root):
    from church_bot.church import add_ministry

    church = Church(root)
    add_ministry(root, "青年牧區")
    edit_ministry(root, "m1", password_hash=hash_password("abcd"))
    latest = church.versions.changes("")[0]
    assert "牧區密碼變更" in latest.summary
    assert all("pbkdf2" not in line for _, line in church.versions.diff(latest))


def test_web_history_page_and_restore(client, paths):
    client.post("/targets/save", data={"name": "新群組", "line_id": gid("7"), "enabled": "on"})
    page = client.get("/history").text
    assert "變更紀錄" in page and "新增「新群組」" in page and "管理網頁" in page
    change = client.app.state.web.church.versions.changes("m1")[0]
    r = client.post(f"/history/{change.id}/restore", follow_redirects=False)
    assert r.status_code == 303 and "level=ok" in r.headers["location"]
    assert all(t.name != "新群組" for t in TargetTable(paths.targets_file).load().items)
    assert client.app.state.web.church.versions.changes("m1")[0].source.startswith("還原")
    # 別的牧區的紀錄不能從這裡還原
    assert "level=error" in client.post(CHURCH + f"/history/{change.id}/restore", follow_redirects=False).headers["location"]
