import pytest

from church_bot.config import Settings, load_settings, save_settings, update_env_file
from church_bot.errors import ConfigError, TableError
from church_bot.models import Member, Target
from church_bot.tables import MemberTable, TargetTable, parse_bool, upsert
from tests.conftest import gid, uid, write


# ------------------------------------------------------------------ tables


def test_target_roundtrip_is_excel_friendly(paths):
    table = TargetTable(paths.targets_file)
    table.save([Target("同工群", gid(), roles=("敬拜", "司琴"), mention=True, note="測試")])
    assert paths.targets_file.read_bytes().startswith(b"\xef\xbb\xbf")  # BOM：Excel 打開不會亂碼
    loaded = table.load()
    assert loaded.items == [Target("同工群", gid(), roles=("敬拜", "司琴"), mention=True, note="測試")]


def test_big5_file_is_read_and_converted(paths):
    paths.targets_file.write_bytes(f"群組名稱,LINE_ID,啟用\n同工群,{gid()},是\n".encode("cp950"))
    result = TargetTable(paths.targets_file).load()
    assert result.items[0].name == "同工群"
    assert any(i.code == "table_reencoded" for i in result.issues)
    assert paths.targets_file.read_bytes().startswith(b"\xef\xbb\xbf")


def test_english_headers_and_bool_words(paths):
    write(paths.targets_file, f"name,line_id,enabled,mention\nA,{gid()},Y,V\nB,{gid('c')},no,\n")
    items = TargetTable(paths.targets_file).load().items
    assert [(t.name, t.enabled, t.mention) for t in items] == [("A", True, True), ("B", False, True)]  # 留空 = 要 @


def test_bad_ids_are_errors_with_row_numbers(paths):
    write(paths.targets_file, "群組名稱,LINE_ID,啟用\n缺ID,,是\n亂填,abc123,是\n")
    issues = {i.code: i for i in TargetTable(paths.targets_file).load().issues}
    assert issues["target_no_id"].is_error and "第 2 列" in issues["target_no_id"].message
    assert issues["target_bad_id"].is_error and "abc123" in issues["target_bad_id"].message
    assert issues["no_active_target"].is_error


def test_missing_required_header_raises(paths):
    write(paths.targets_file, "名稱錯了,隨便\nA,B\n")
    with pytest.raises(TableError):
        TargetTable(paths.targets_file).load()


def test_member_bad_user_id_and_duplicate_alias(paths):
    write(paths.members_file, f"名字,其他寫法,LINE_userId\n陳小明,小明,{gid()}\n王小明,小明,\n")
    result = MemberTable(paths.members_file).load()
    codes = {i.code for i in result.issues}
    assert {"member_bad_uid", "member_dup_name"} <= codes
    assert result.items[0].line_user_id == ""


def test_upsert_rename_and_collision():
    a, b = Member("A"), Member("B")
    assert [m.name for m in upsert([a, b], Member("A2"), key=lambda m: m.name, original_key="A")] == ["A2", "B"]
    assert [m.name for m in upsert([a, b], Member("B", note="new"), key=lambda m: m.name, original_key="A")] == ["B"]
    assert [m.name for m in upsert([a], Member("C"), key=lambda m: m.name)] == ["A", "C"]


@pytest.mark.parametrize(("text", "expected"), [("是", True), ("否", False), ("", True), ("✓", True), ("??", None)])
def test_parse_bool(text, expected):
    assert parse_bool(text, default=True) is expected


# ------------------------------------------------------------------ settings / .env


def test_defaults_without_files(paths):
    settings = load_settings(paths)
    assert settings.schedule.time == "20:00" and settings.source.kind == "csv"


def test_secrets_come_from_env_file_and_are_never_saved(paths):
    write(paths.env_file, "# comment\nLINE_CHANNEL_ACCESS_TOKEN=tok-123\n")
    settings = load_settings(paths)
    assert settings.line.channel_access_token == "tok-123"
    save_settings(paths, settings)
    assert "tok-123" not in paths.settings_file.read_text(encoding="utf-8")


def test_update_env_file_keeps_comments(paths):
    write(paths.env_file, "# 說明\nLINE_CHANNEL_ACCESS_TOKEN=old\nOTHER=1\n")
    update_env_file(paths.env_file, {"LINE_CHANNEL_ACCESS_TOKEN": "new", "UI_PASSWORD": "pw"})
    assert paths.env_file.read_text(encoding="utf-8") == "# 說明\nLINE_CHANNEL_ACCESS_TOKEN=new\nOTHER=1\nUI_PASSWORD=pw\n"


def test_broken_yaml_gives_line_number(paths):
    write(paths.settings_file, "schedule:\n  time: 20:00\n   bad indent: [\n")
    with pytest.raises(ConfigError) as exc:
        load_settings(paths)
    assert "行" in exc.value.message and exc.value.hint


def test_invalid_values_are_explained(paths):
    write(paths.settings_file, "schedule:\n  time: '25:99'\n")
    with pytest.raises(ConfigError) as exc:
        load_settings(paths)
    assert "24 小時制" in exc.value.message


def test_settings_roundtrip(paths):
    settings = Settings()
    settings.schedule.day_of_week = "fri"
    settings.message.role_order = ["講員", "司琴"]
    save_settings(paths, settings)
    loaded = load_settings(paths)
    assert loaded.schedule.day_of_week == "fri" and loaded.message.role_order == ["講員", "司琴"]
    assert uid()  # 讓 import 被使用（避免 lint 抱怨）
