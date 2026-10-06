"""多牧區：牧區清單、自動搬家、群組屬於哪個牧區、還沒分配的群組。"""

import pytest
import yaml

from church_bot.church import (
    add_ministry, check_password, edit_ministry, hash_password, load_church, migrate_legacy, remove_ministry,
)
from church_bot.config import Paths, load_settings, save_settings
from church_bot.core.history import History
from church_bot.errors import ConfigError
from church_bot.ministries import Church
from church_bot.models import Member, Target
from church_bot.tables import MemberTable, TargetTable
from tests.conftest import gid, uid, write


def test_ministry_paths_are_separate_folders(root):
    m1 = root.for_ministry("m1")
    assert m1.targets_file == root.root / "config" / "ministries" / "m1" / "targets.csv"
    assert m1.db_file == root.root / "data" / "ministries" / "m1" / "church_bot.db"
    assert m1.env_file == root.env_file and m1.log_file == root.log_file  # 金鑰、記錄檔整個教會一份
    assert m1.church == root and m1.shared_db_file == root.db_file


def test_add_rename_and_remove_ministries(root):
    youth = add_ministry(root, "青年牧區", "王小明、林美華")
    adult = add_ministry(root, " 壯年 牧區 ")
    assert (youth.id, adult.id, adult.name) == ("m1", "m2", "壯年 牧區")
    assert root.for_ministry("m2").settings_file.exists()
    assert load_settings(root.for_ministry("m2")).schedule.enabled is False  # 新牧區先不自動發
    with pytest.raises(ConfigError, match="已經有"):
        add_ministry(root, "青年牧區")
    with pytest.raises(ConfigError, match="已經有"):
        edit_ministry(root, "m2", name="青年牧區")
    edit_ministry(root, "m2", name="約書亞牧區")
    assert load_church(root).find("約書亞牧區").id == "m2"
    assert load_church(root).find("ｍ1").id == "m1"  # 編號全形也找得到
    moved = remove_ministry(root, "m1")
    assert moved.exists() and not root.for_ministry("m1").config_dir.exists()
    assert [m.id for m in load_church(root).ministries] == ["m2"]
    assert add_ministry(root, "新的").id == "m3"  # 編號不會重複使用


def test_second_layer_password_hash():
    stored = hash_password("abc123")
    assert check_password("abc123", stored) and not check_password("abc124", stored)
    assert not check_password("abc123", "壞掉的")


def test_legacy_single_ministry_is_migrated(root):
    TargetTable(root.targets_file).save([Target("同工群", gid())])
    MemberTable(root.members_file).save([Member("王小明", line_user_id=uid())])
    settings = load_settings(root)
    settings.web.port = 9001
    settings.message.title = "青年崇拜服事提醒"
    settings.schedule.enabled = False  # 舊版由 Telegram 呼叫 cli.bat send 時的設定
    save_settings(root, settings)
    History(root.db_file).remember_person(uid(), "小明", gid())

    ministry = migrate_legacy(root, "青年牧區")
    assert (ministry.id, ministry.name) == ("m1", "青年牧區")
    m1 = root.for_ministry("m1")
    assert [t.name for t in TargetTable(m1.targets_file).load().items] == ["同工群"]
    assert load_settings(m1).message.title == "青年崇拜服事提醒"
    assert load_settings(m1).schedule.enabled  # 打開：不然 Telegram 不帶牧區的 send 會什麼都不發
    assert load_settings(m1).web.port == 9001 and load_settings(root).web.port == 9001  # host/port 搬到 church.yaml
    assert "web" not in yaml.safe_load(m1.settings_file.read_text(encoding="utf-8"))
    assert not root.targets_file.exists()
    assert (root.config_dir / "_before_ministries" / "targets.csv").exists()
    assert History(m1.db_file).person(uid())["display_name"] == "小明"  # 收集到的人跟著牧區走
    assert History(root.db_file).person(uid()) is None
    assert migrate_legacy(root) is None  # 只搬一次


def test_fresh_install_is_not_migrated(root):
    assert migrate_legacy(root) is None and not root.church_file.exists()


@pytest.fixture
def church(root):
    add_ministry(root, "青年牧區")
    add_ministry(root, "壯年牧區")
    TargetTable(root.for_ministry("m1").targets_file).save([Target("青年同工", gid("1"))])
    TargetTable(root.for_ministry("m2").targets_file).save(
        [Target("壯年舊群", gid("2"), enabled=False), Target("壯年同工", gid("3"))])
    return Church(root)


def test_owner_of_group(church):
    assert church.owner_of(gid("1")).name == "青年牧區"
    assert church.owner_of(gid("2")).name == "壯年牧區"  # 停用的也算
    assert church.owner_of(gid("9")) is None


def test_services_share_one_church_database(church):
    youth, adult = church.service("m1"), church.service("m2")
    assert youth is church.service("m1")  # 同一個程式裡一個牧區只有一份
    assert youth.history.db_path != adult.history.db_path
    assert youth.shared is adult.shared is church.shared


def test_unassigned_group_is_handed_over_with_its_people(church):
    church.shared.remember_chat(gid("8"), "group", "新來的小組")
    church.shared.remember_person(uid("8"), "阿恩", gid("8"))
    church.shared.claim_real_name(uid("8"), "林恩")
    assert [c["name"] for c in church.unassigned_chats()] == ["新來的小組"]

    target = church.assign_chat(gid("8"), "壯年牧區")
    assert (target.name, target.enabled) == ("新來的小組", False)
    assert church.owner_of(gid("8")).name == "壯年牧區"
    assert church.unassigned_chats() == []
    adult = church.service("m2").history
    assert adult.person(uid("8"))["real_name"] == "林恩" and church.shared.person(uid("8")) is None
    with pytest.raises(ConfigError, match="已經在"):
        church.assign_chat(gid("8"), "青年牧區")


def test_people_and_admins_are_looked_up_per_ministry(church, root):
    MemberTable(root.for_ministry("m1").members_file).save([Member("王小明", line_user_id=uid("1"), admin=True)])
    church.service("m2").history.remember_person(uid("1"), "Julia", gid("3"))
    assert [u.name for u in church.units_of_person(uid("1"))] == ["青年牧區", "壯年牧區"]
    assert [u.name for u in church.admin_units(uid("1"))] == ["青年牧區"]
    assert church.units_of_person(uid("7")) == []


def test_relative_paths_prefer_the_ministry_folder(root):
    m1 = root.for_ministry("m1")
    write(root.config_dir / "shared.csv", "x")
    write(m1.config_dir / "own.csv", "x")
    assert m1.resolve("own.csv") == m1.config_dir / "own.csv"
    assert m1.resolve("config/shared.csv") == root.config_dir / "shared.csv"
    assert root.resolve("own.csv") == root.root / "own.csv"


def test_init_creates_the_first_ministry_with_examples(root):
    from church_bot.cli import cmd_init
    from tests.conftest import REPO_ROOT

    for name in ("settings.example.yaml", "targets.example.csv", "members.example.csv", "teams.example.csv"):
        (root.config_dir / name).write_bytes((REPO_ROOT / "config" / name).read_bytes())
    (root.root / ".env.example").write_bytes((REPO_ROOT / ".env.example").read_bytes())
    assert cmd_init(root, None) == 0
    m1 = root.for_ministry("m1")
    assert [m.name for m in load_church(root).ministries] == ["第一個牧區"]
    assert m1.targets_file.exists() and (m1.config_dir / "roster.demo.csv").exists()
    assert load_settings(m1).source.csv_path == "roster.demo.csv"
    assert Church(root).service("m1").preview()[1].days  # 範例服事表讀得到
    cmd_init(root, None)  # 再跑一次不會多出第二個牧區
    assert len(load_church(root).ministries) == 1
