from urllib.parse import unquote

from church_bot.config import load_settings
from church_bot.core.accounts import build_accounts
from church_bot.core.history import History, nicknames_of
from church_bot.models import Member, Team
from church_bot.tables import MemberTable, TeamTable
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
    assert "開放中" in client.get("/members/accounts").text
    client.post("/members/collect", data={"enabled": "0"})
    assert not load_settings(paths).chat.collect_names


def test_add_account_with_claimed_name(client, paths):
    seen(paths, uid("7"), "小張", "張三豐")
    assert "加入「張三豐」" in client.get("/members/accounts").text
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
    assert "改名成「林美樺」" in client.get("/members/accounts").text
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


# ------------------------------------------------------------------ 其他寫法：一個一個加／刪


def test_aliases_can_be_added_and_removed_one_at_a_time(client, paths):
    client.post("/members/aliases/add", data={"name": "陳小明", "alias": "阿明"})
    assert "阿明" in members(paths)["陳小明"].aliases
    client.post("/members/aliases/remove", data={"name": "陳小明", "alias": "小明"})
    stored = members(paths)["陳小明"]
    assert "阿明" in stored.aliases and "小明" not in stored.aliases  # 其他的寫法留著，一人多名


def test_alias_cannot_be_taken_from_someone_else(client, paths):
    r = client.post("/members/aliases/add", data={"name": "陳小明", "alias": "美華"})
    assert "已經是同工「林美華」" in r.text
    assert "美華" not in members(paths)["陳小明"].aliases

    r = client.post("/members/aliases/add", data={"name": "陳小明", "alias": "小明"})
    assert "已經是「陳小明」的寫法" in r.text


# ------------------------------------------------------------------ 登記的暱稱 → 其他寫法


def nickname(paths, user: str, nick: str) -> None:
    History(paths.db_file).claim_nickname(user, nick)


def test_registered_nickname_becomes_an_alias_of_the_linked_member(client, paths):
    seen(paths, uid("7"), "美美")
    client.post("/members/accounts/link", data={"user_id": uid("7"), "member_name": "林美華"})
    nickname(paths, uid("7"), "美美姐")
    nickname(paths, uid("7"), "華姐")
    page = client.get("/members/accounts").text
    assert "暱稱 美美姐" in page and "加成其他寫法" in page

    client.post("/members/accounts/nickname", data={"user_id": uid("7"), "nickname": "美美姐"})
    assert "美美姐" in members(paths)["林美華"].aliases
    assert nicknames_of(History(paths.db_file).person(uid("7"))) == ("華姐",)  # 處理過的就不再出現

    client.post("/members/accounts/nickname/drop", data={"user_id": uid("7"), "nickname": "華姐"})
    assert nicknames_of(History(paths.db_file).person(uid("7"))) == ()
    assert "華姐" not in members(paths)["林美華"].aliases


def test_nickname_needs_the_account_to_be_linked_first(client, paths):
    seen(paths, uid("7"), "路人")
    nickname(paths, uid("7"), "阿路")
    r = client.post("/members/accounts/nickname", data={"user_id": uid("7"), "nickname": "阿路"})
    assert "還沒對應到同工" in r.text
    assert nicknames_of(History(paths.db_file).person(uid("7"))) == ("阿路",)


def test_nickname_refuses_to_collide_with_another_member(client, paths):
    seen(paths, uid("7"), "美美")
    client.post("/members/accounts/link", data={"user_id": uid("7"), "member_name": "林美華"})
    nickname(paths, uid("7"), "小明")  # 已經是陳小明的其他寫法
    r = client.post("/members/accounts/nickname", data={"user_id": uid("7"), "nickname": "小明"})
    assert "已經是同工「陳小明」" in r.text
    assert "小明" not in members(paths)["林美華"].aliases


# ------------------------------------------------------------------ 小團


def teams(paths) -> dict[str, Team]:
    return {t.name: t for t in TeamTable(paths.teams_file).load().items}


def test_team_can_be_added_edited_and_deleted_from_the_web(client, paths):
    client.post("/teams/save", data={"name": "青年實體團", "aliases": "青年團", "members": "張晨光、小明",
                                     "active": "on", "note": "組合式"})
    stored = teams(paths)["青年實體團"]
    assert stored.aliases == ("青年團",) and stored.members == ("張晨光", "小明")
    assert "青年實體團" in client.get("/members/teams").text

    client.post("/teams/save", data={"name": "青年小團", "members": "張晨光", "active": "on",
                                     "original_name": "青年實體團"})
    assert "青年實體團" not in teams(paths) and "青年小團" in teams(paths)

    client.post("/teams/delete", data={"name": "青年小團"})
    assert "青年小團" not in teams(paths)

    for name in list(teams(paths)):  # 全部刪掉（不用小團功能的教會）畫面也要正常
        client.post("/teams/delete", data={"name": name})
    assert "還沒有任何小團" in client.get("/members/teams").text


def test_team_name_may_not_be_a_member_name(client, paths):
    r = client.post("/teams/save", data={"name": "小明", "members": "林美華", "active": "on"})
    assert "已經是同工「陳小明」" in r.text
    assert "小明" not in teams(paths)


def test_team_members_must_be_on_the_roster(client, paths):
    r = client.post("/teams/save", data={"name": "新團", "members": "張晨光、還沒登記的人", "active": "on"}, follow_redirects=False)
    assert "level=error" in r.headers["location"] and "新團" not in teams(paths)
    client.post("/teams/save", data={"name": "新團", "members": "張晨光、小明", "active": "on"})  # 其他寫法也算名單上的人
    assert teams(paths)["新團"].members == ("張晨光", "小明")
    page = client.get("/members/teams?edit_team=新團").text
    assert "data-picklist" in page and 'value="張晨光、小明"' in page  # 清單挑選：現有成員在隱藏欄位裡


def test_members_page_warns_about_team_members_who_are_not_on_the_list(client, paths):
    from church_bot.models import Team
    from church_bot.tables import TeamTable

    TeamTable(paths.teams_file).save([Team("新團", members=("還沒登記的人",))])  # 例如用 Excel 直接改檔
    assert "的成員「還沒登記的人」不在同工名單上" in client.get("/members/teams").text


# ------------------------------------------------------------------ LINE 群組頁：照服事表挑「只發這些服事」


def test_targets_page_lists_the_roles_that_exist_on_the_roster(client):
    page = client.get("/targets?new=1").text  # 新增／編輯的表單畫面才會列服事項目
    for role in ("講員", "司琴", "招待"):  # 範例服事表的欄位；點一下就加進去，不會打錯字
        assert f'data-pick-value="{role}"' in page
    assert "把大群拆成敬拜團群、招待群，各收自己的" in client.get("/targets").text


def test_targets_page_still_works_when_the_roster_cannot_be_read(client, paths, monkeypatch):
    from church_bot.errors import SourceError
    from church_bot.service import BotService

    def broken(self, settings, today, use_cache=False):
        raise SourceError("讀不到服事表", "檢查網址")

    monkeypatch.setattr(BotService, "fetch_roster", broken)
    assert client.get("/targets").status_code == 200


# ------------------------------------------------------------------ 「/點名」之後：名字對得上的一次全部對應


def test_exact_links_skip_guesses_and_two_people_claiming_the_same_name():
    from church_bot.core.accounts import exact_links

    names = [Member("陳小明"), Member("林美華"), Member("黃喜樂", line_user_id=uid("9"))]
    accounts = build_accounts([
        person(uid("1"), real="陳小明"),
        person(uid("2"), display="林美華"),  # 只是 LINE 名稱一樣：不算，要管理員看
        person(uid("3"), real="黃喜樂"),  # 那位已經有帳號了：衝突
        person(uid("4"), real="王大衛"), person(uid("5"), real="王大衛"),  # 名單上沒有
    ], names)
    assert [a.user_id for a in exact_links(accounts)] == [uid("1")]
    two = build_accounts([person(uid("1"), real="陳小明"), person(uid("6"), real="小明哥")],
                         [Member("陳小明", ("小明哥",))])
    assert exact_links(two) == []  # 兩個帳號都說自己是陳小明：一個一個判斷


def test_link_all_button(client, paths):
    seen(paths, uid("1"), "Ming", real="陳小明")
    seen(paths, uid("2"), "美華", real="林美華")
    page = client.get("/members/accounts").text
    assert "名字對得上的 2 位全部對應" in page
    r = client.post("/members/accounts/link-all", follow_redirects=False)
    assert "已對應 2 位" in unquote(r.headers["location"])
    found = members(paths)
    assert found["陳小明"].line_user_id == uid("1") and found["林美華"].line_user_id == uid("2")
    assert "全部對應" not in client.get("/members/accounts").text
    assert "level=warn" in client.post("/members/accounts/link-all", follow_redirects=False).headers["location"]
