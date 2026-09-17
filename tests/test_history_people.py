import datetime as dt
import sqlite3

from church_bot.core.history import History
from tests.conftest import gid, uid

OLD_SCHEMA = """
CREATE TABLE chats (chat_id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active', first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
CREATE TABLE people (user_id TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '',
    chat_id TEXT NOT NULL DEFAULT '', first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
"""


def test_old_database_is_upgraded_in_place(paths):
    conn = sqlite3.connect(paths.db_file)
    conn.executescript(OLD_SCHEMA)
    conn.execute("INSERT INTO chats VALUES (?, 'group', '敬拜團', 'active', 't', 't')", (gid(),))
    conn.execute("INSERT INTO people VALUES (?, '小美', ?, 't', 't')", (uid(), gid()))
    conn.execute("INSERT INTO people VALUES (?, '阿豪', ?, 't', 't')", (uid("c"), uid("c")))  # 私訊，不是群組
    conn.commit()
    conn.close()

    history = History(paths.db_file)
    person = history.person(uid())
    assert person["display_name"] == "小美" and person["real_name"] == "" and person["ignored"] == 0
    by_id = {p["user_id"]: p for p in history.people()}
    assert by_id[uid()]["chat_names"] == "敬拜團"
    assert by_id[uid("c")]["chat_names"] is None
    History(paths.db_file)  # 再開一次不會重複搬資料或出錯


def test_person_seen_in_several_groups(paths):
    history = History(paths.db_file)
    history.remember_chat(gid(), "group", "敬拜團")
    history.remember_person(uid(), "小美", gid())
    history.remember_person(uid(), "", gid("d"))
    person = history.people()[0]
    assert person["display_name"] == "小美"  # 空白名稱不會蓋掉已知名稱
    assert person["chat_id"] == gid("d")
    assert set(person["chat_names"].split("、")) == {"敬拜團", gid("d")}


def test_real_name_claim_overwrites_and_unignores(paths):
    history = History(paths.db_file)
    history.remember_person(uid(), "小美", gid())
    history.set_person_ignored(uid(), True)
    history.claim_real_name(uid(), "林美")
    history.claim_real_name(uid(), "林美華")
    person = history.person(uid())
    assert person["real_name"] == "林美華" and person["ignored"] == 0 and person["display_name"] == "小美"
    history.clear_real_name(uid())
    assert history.person(uid())["real_name"] == ""


def test_claim_from_someone_never_seen_creates_the_person(paths):
    history = History(paths.db_file)
    history.claim_real_name(uid(), "陳小明")
    assert history.person(uid())["real_name"] == "陳小明"


def test_forget_person_removes_memberships(paths):
    history = History(paths.db_file)
    history.remember_person(uid(), "小美", gid())
    history.forget_person(uid())
    history.remember_person(uid(), "小美", gid("d"))
    assert history.people()[0]["chat_names"] == gid("d")


def test_member_counts(paths):
    history = History(paths.db_file)
    history.remember_chat(gid(), "group", "敬拜團")
    history.set_member_count(gid(), 25)
    history.set_member_count(gid("d"), 8)
    counts = history.member_counts()
    assert counts[gid()][0] == 25 and counts[gid("d")][0] == 8
    assert dt.datetime.fromisoformat(counts[gid()][1])
    names = {c["chat_id"]: c["name"] for c in history.chats()}
    assert names[gid()] == "敬拜團"  # 更新人數不會洗掉原本記的群組名稱
