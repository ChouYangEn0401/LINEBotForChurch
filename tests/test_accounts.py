from church_bot.config import load_settings
from church_bot.core.accounts import build_accounts
from church_bot.core.history import History
from church_bot.models import Member
from church_bot.tables import MemberTable
from tests.conftest import gid, uid


def person(user: str, display: str = "", real: str = "") -> dict:
    return {"user_id": user, "display_name": display, "real_name": real, "last_seen": "2026-09-16T10:00:00+08:00"}


# ------------------------------------------------------------------ pure logic


def test_account_actions():
    members = [Member("陳小明", ("小明",), line_user_id=uid("1")), Member("林美華", ("美華",)),
               Member("王大衛", line_user_id=uid("2"))]
    accounts = {a.user_id: a for a in build_accounts([
        person(uid("1"), "Ming"),  # 已對應、沒登記
        person(uid("2"), "大衛", "王大偉"),  # 已對應，但登記了不同的名字 → 改名
        person(uid("3"), "小美", "美華"),  # 登記的名字是名單上的其他寫法 → 對應
        person(uid("4"), "美華"),  # 沒登記，只有 LINE 名稱對得上 → 對應（提醒只是名稱一樣）
        person(uid("5"), "阿明", "陳小明"),  # 登記的名字已經是別人的帳號 → 要確認
        person(uid("6"), "小明"),  # LINE 名稱撞到已經有帳號的人 → 不算衝突，當作新的人
        person(uid("7"), "路人", "張三"),  # 名單上沒有 → 加入
    ], members)}
    assert {k[-1]: a.action for k, a in accounts.items()} == {
        "1": "linked", "2": "rename", "3": "link", "4": "link", "5": "conflict", "6": "add", "7": "add"}
    assert accounts[uid("3")].match.name == "林美華" and accounts[uid("3")].matched_by == "real_name"
    assert accounts[uid("4")].matched_by == "display_name"
    assert not accounts[uid("1")].needs_review and not accounts[uid("6")].needs_review
    assert accounts[uid("7")].needs_review and accounts[uid("7")].proposed_name == "張三"


def test_claims_waiting_for_review_are_listed_first():
    ordered = build_accounts([person(uid("1"), "A"), person(uid("2"), "B", "乙"), person(uid("3"), "C")], [])
    assert [a.user_id for a in ordered][0] == uid("2")


# ------------------------------------------------------------------ web


def members(paths) -> dict[str, Member]:
    return {m.name: m for m in MemberTable(paths.members_file).load().items}


def seen(paths, user: str, display: str, real: str = "") -> None:
    history = History(paths.db_file)
    history.remember_person(user, display, gid())
    if real:
        history.claim_real_name(user, real)


def test_collect_toggle_is_saved(client, paths):
    client.post("/members/collect", data={"enabled": "1"})
    assert load_settings(paths).chat.collect_names
    assert "開放中" in client.get("/members").text
    client.post("/members/collect", data={"enabled": "0"})
    assert not load_settings(paths).chat.collect_names


def test_add_account_with_claimed_name(client, paths):
    seen(paths, uid("7"), "小張", "張三豐")
    assert "加入「張三豐」" in client.get("/members").text
    client.post("/members/accounts/add", data={"user_id": uid("7")})
    added = members(paths)["張三豐"]
    assert added.line_user_id == uid("7") and added.active and "小張" in added.note
    assert History(paths.db_file).person(uid("7"))["real_name"] == ""


def test_add_account_without_claim_starts_inactive(client, paths):
    seen(paths, uid("7"), "小張")
    client.post("/members/accounts/add", data={"user_id": uid("7")})
    assert not members(paths)["小張"].active


def test_add_refuses_a_name_already_on_the_roster(client, paths):
    seen(paths, uid("7"), "小明本人", "小明")  # 範例名單裡「小明」是陳小明的其他寫法
    before = members(paths)
    r = client.post("/members/accounts/add", data={"user_id": uid("7")})
    assert "已經有「陳小明」" in r.text
    assert members(paths) == before


def test_link_rename_and_ignore(client, paths):
    seen(paths, uid("7"), "美美", "林美華")
    client.post("/members/accounts/link", data={"user_id": uid("7"), "member_name": "林美華"})
    assert members(paths)["林美華"].line_user_id == uid("7")

    History(paths.db_file).claim_real_name(uid("7"), "林美樺")
    assert "改名成「林美樺」" in client.get("/members").text
    client.post("/members/accounts/rename", data={"user_id": uid("7")})
    renamed = members(paths)["林美樺"]
    assert renamed.line_user_id == uid("7") and "林美華" in renamed.aliases and "美華" in renamed.aliases

    History(paths.db_file).claim_real_name(uid("7"), "亂填")
    client.post("/members/accounts/ignore", data={"user_id": uid("7")})
    stored = History(paths.db_file).person(uid("7"))
    assert stored["real_name"] == "" and stored["ignored"] == 0  # 已經是同工：只略過這次登記，不隱藏

    seen(paths, uid("8"), "路人")
    client.post("/members/accounts/ignore", data={"user_id": uid("8")})
    assert History(paths.db_file).person(uid("8"))["ignored"] == 1
    client.post("/members/accounts/unignore", data={"user_id": uid("8")})
    assert History(paths.db_file).person(uid("8"))["ignored"] == 0


def test_rename_refuses_to_collide_with_another_member(client, paths):
    seen(paths, uid("7"), "美美", "林美華")
    client.post("/members/accounts/link", data={"user_id": uid("7"), "member_name": "林美華"})
    History(paths.db_file).claim_real_name(uid("7"), "陳小明")
    r = client.post("/members/accounts/rename", data={"user_id": uid("7")})
    assert "已經是同工「陳小明」" in r.text
    assert "林美華" in members(paths)
