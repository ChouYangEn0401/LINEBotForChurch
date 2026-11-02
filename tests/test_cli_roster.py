"""``roster show``／``roster image``：上兩週＋下四週的服事表，疊完人員調換；畫成 PNG。"""

from __future__ import annotations

import pytest

from church_bot import roster_image
from tests.cli_setup import fix_now, run, setup_ministry


@pytest.fixture
def ministry(paths, monkeypatch):
    setup_ministry(paths)
    fix_now(monkeypatch)
    return paths


def test_roster_show_is_the_calendar_window_with_overrides(ministry, capsys):
    code, out = run(ministry.church, "roster", "show", capsys=capsys)
    assert code == 0, out
    assert out["ok"] and out["ministry"] == {"id": "m1", "name": "測試牧區"}
    assert out["today"] == "2026-09-11" and out["column"] == "服飾"
    # 跟 /行事曆 一樣：本週日 9/6 往前兩週、往後四週 → 8/23 ～ 10/3
    assert out["window"] == {"start": "2026-08-23", "end": "2026-10-03", "weeks_back": 2, "weeks_forward": 4}
    assert [d["date"] for d in out["days"]] == ["2026-08-30", "2026-09-06", "2026-09-13", "2026-09-20", "2026-09-27"]
    sunday = out["days"][2]
    assert sunday["weekday"] == "日" and sunday["label"] == "主日崇拜"
    assert sunday["texts"] == [["服飾", "深色西裝"]]
    piano = next(a for a in sunday["assignments"] if a["role"] == "司琴")
    assert piano == {"role": "司琴", "names": ["陳小明"], "raw": ["小明"], "adjusted": False}
    assert sunday["adjusted"] is False and out["days"][1]["this_week"] is True and out["days"][0]["past"] is True

    assert run(ministry.church, "swap", "person", "--date=2026-09-13", "--role=司琴", "--from=小明", "--to=美華",
               "--by=telegram:42", capsys=capsys)[0] == 0
    _code, out = run(ministry.church, "roster", "show", "--weeks-back=0", "--weeks-forward=2", capsys=capsys)
    assert [d["date"] for d in out["days"]] == ["2026-09-06", "2026-09-13"]
    sunday = out["days"][1]
    piano = next(a for a in sunday["assignments"] if a["role"] == "司琴")
    assert piano["names"] == ["林美華"] and piano["adjusted"] is True and sunday["adjusted"] is True
    assert out["overrides"] == {"active": 1, "applied": 1}


def test_roster_show_text_for_people(ministry, capsys):
    from church_bot.cli import build_parser, main  # noqa: F401
    from church_bot.cli_api import COMMANDS

    args = build_parser().parse_args(["roster", "show"])
    assert COMMANDS["roster"](ministry.church, args) == 0
    out = capsys.readouterr().out
    assert "9/13（日） 主日崇拜" in out and "司琴：陳小明" in out and "服飾：深色西裝" in out


def test_roster_show_when_the_sheet_cannot_be_read(ministry, capsys):
    (ministry.config_dir / "roster.csv").unlink()
    code, out = run(ministry.church, "roster", "show", capsys=capsys)
    assert code in (1, 2) and out["ok"] is False and out["message"]


def test_roster_image_writes_a_png(ministry, tmp_path, capsys):
    run(ministry.church, "swap", "weeks", "--a=2026-09-13", "--b=2026-09-20", "--column=服飾", "--by=telegram:42",
        capsys=capsys)
    target = tmp_path / "out" / "roster.png"
    code, out = run(ministry.church, "roster", "image", f"--out={target}", capsys=capsys)
    assert code == 0, out
    assert out["path"] == str(target) and out["days"] == 5 and out["font"]
    data = target.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    from PIL import Image

    with Image.open(target) as img:
        assert img.width == roster_image.WIDTH and img.height > 400


def test_roster_image_without_a_chinese_font_says_fallback(ministry, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(roster_image, "find_font", lambda: None)
    code, out = run(ministry.church, "roster", "image", f"--out={tmp_path / 'r.png'}", capsys=capsys)
    assert code == 0 and out["font"] == "fallback" and (tmp_path / "r.png").exists()


def test_roster_image_with_nothing_in_the_window(paths, monkeypatch, tmp_path, capsys):
    setup_ministry(paths, "日期,講員\n2026/1/4,王牧師\n")
    fix_now(monkeypatch)
    code, out = run(paths.church, "roster", "image", f"--out={tmp_path / 'empty.png'}", capsys=capsys)
    assert code == 0 and out["days"] == 0 and (tmp_path / "empty.png").exists()
