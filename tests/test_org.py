from church_bot.config import load_settings, save_settings
from church_bot.core.history import History
from church_bot.models import Member, Target
from church_bot.org import OrgTable, OrgUnit, build_org, suggest_plan
from tests.conftest import FakeMessenger, gid, uid, write

TARGETS = [
    Target("北區同工群", gid("1")), Target("北一小組", gid("2")), Target("北二小組", gid("3"), enabled=False),
    Target("南區同工群", gid("4")), Target("行政同工", gid("5")), Target("我自己", uid("9")),
]
MEMBERS = [Member("陳小明", ("小明",)), Member("林美華"), Member("王大衛"), Member("周以琳", active=False)]
UNITS = [
    OrgUnit("北區", kind="牧區", leader="王大衛", groups=("北區同工群",), members=("王大衛",)),
    OrgUnit("北一小組", parent="北區", groups=("北一小組",), members=("小明", "林美華")),
    OrgUnit("北二小組", parent="北區", groups=("北二小組",), members=("林美華",)),
    OrgUnit("南區", groups=("南區同工群", "我自己")),
]
SIZES = {gid("1"): 30, gid("2"): 8, gid("3"): 9}


def by_name(nodes, found=None):
    found = {} if found is None else found
    for node in nodes:
        found[node.unit.name] = node
        by_name(node.children, found)
    return found


def test_tree_rolls_up_groups_people_members_and_messages():
    report = build_org(UNITS, TARGETS, MEMBERS, SIZES, every_n_weeks=1)
    nodes = by_name(report.roots)
    assert [n.unit.name for n in report.roots] == ["北區", "南區"]
    assert [c.unit.name for c in nodes["北區"].children] == ["北一小組", "北二小組"]
    assert nodes["北一小組"].depth == 1 and nodes["北一小組"].members == ["陳小明", "林美華"]

    north = nodes["北區"].totals
    assert (north.groups, north.active_groups, north.people) == (3, 2, 38)  # 北二小組停用，不算人數
    assert north.members == 3  # 林美華在兩個小組，只算一次
    assert north.monthly == round(38 * 52 / 12)

    south = nodes["南區"].totals
    assert south.unknown_sizes == 1 and south.people == 1  # 南區同工群還沒查人數；個人 ID 算 1 人


def test_church_totals_include_unassigned_groups_and_biweekly_halves_messages():
    weekly = build_org(UNITS, TARGETS, MEMBERS, SIZES, every_n_weeks=1)
    assert [g.target.name for g in weekly.unassigned_groups] == ["行政同工"]
    assert weekly.unassigned_members == []  # 周以琳停用，不列
    assert weekly.church.active_groups == 5 and weekly.church.unknown_sizes == 2
    biweekly = build_org(UNITS, TARGETS, MEMBERS, SIZES, every_n_weeks=2)
    assert biweekly.church.monthly == round(weekly.church.people * 52 / 12 / 2)


def test_problems_are_reported_not_fatal():
    units = [
        OrgUnit("A", parent="B"), OrgUnit("B", parent="A"),  # 繞圈
        OrgUnit("C", parent="不存在", groups=("北區同工群", "沒有這個群組"), members=("路人",)),
        OrgUnit("D", groups=("北區同工群",)),  # 同一個群組放兩個地方
    ]
    report = build_org(units, TARGETS, MEMBERS, SIZES)
    codes = [i.code for i in report.issues]
    assert {"org_cycle", "org_missing_parent", "org_unknown_group", "org_unknown_member", "org_group_twice"} <= set(codes)
    assert {n.unit.name for n in report.roots} >= {"C", "D"}
    assert by_name(report.roots)["D"].groups == []
    assert len(by_name(report.roots)) == 4  # 繞圈的單位也不會消失


def test_plan_suggestion():
    assert "輕用量" in suggest_plan(200)
    assert "中用量" in suggest_plan(201)
    assert "高用量" in suggest_plan(6000)
    assert "加購" in suggest_plan(6001)


def test_descendants_and_group_owners():
    report = build_org(UNITS, TARGETS, MEMBERS, SIZES)
    assert report.descendants("北區") == {"北一小組", "北二小組"}
    assert report.group_owners()["北一小組"] == "北一小組" and report.group_owners()["我自己"] == "南區"


def test_org_table_roundtrip_and_missing_file_is_quiet(paths):
    table = OrgTable(paths.org_file)
    assert table.load().items == [] and table.load().issues == []
    table.save(UNITS)
    assert table.load().items == UNITS


# ------------------------------------------------------------------ web


def test_lab_page_add_rename_and_delete_units(client, paths):
    assert "還沒有任何單位" in client.get("/lab/org").text
    client.post("/lab/org/save", data={"name": "北區", "kind": "牧區", "groups": ["主日服事同工群"]})
    client.post("/lab/org/save", data={"name": "北一小組", "parent": "北區", "members": "陳小明、美華"})
    page = client.get("/lab/org").text
    assert "北區" in page and "北一小組" in page and "林美華" in page  # 其他寫法對到正式名字

    client.post("/lab/org/save", data={"name": "北部牧區", "original_name": "北區", "kind": "牧區"})
    units = {u.name: u for u in OrgTable(paths.org_file).load().items}
    assert units["北一小組"].parent == "北部牧區"

    r = client.post("/lab/org/save", data={"name": "北部牧區", "original_name": "北部牧區", "parent": "北一小組"})
    assert "不能選自己或自己底下的單位" in r.text

    client.post("/lab/org/save", data={"name": "北一小組-1", "parent": "北一小組"})
    client.post("/lab/org/delete", data={"name": "北一小組"})
    units = {u.name: u for u in OrgTable(paths.org_file).load().items}
    assert "北一小組" not in units and units["北一小組-1"].parent == "北部牧區"


def test_refresh_group_sizes(client, paths, monkeypatch):
    settings = load_settings(paths)
    settings.messenger.kind = "line"
    save_settings(paths, settings)
    write(paths.targets_file, f"群組名稱,LINE_ID,啟用\n同工群,{gid('1')},是\n我,{uid()},是\n")
    fake = FakeMessenger()
    fake.sizes = {gid("1"): 42}
    monkeypatch.setattr("church_bot.web.app.build_messenger", lambda s, p: fake)
    r = client.post("/lab/org/refresh-sizes")
    assert "更新了 1 個群組的人數" in r.text
    assert History(paths.db_file).member_counts()[gid("1")][0] == 42


def test_refresh_sizes_in_test_mode_explains_why_not(client):
    assert "測試模式" in client.post("/lab/org/refresh-sizes").text
