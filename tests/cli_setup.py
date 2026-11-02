"""給 ClawBot 用的指令（swap、roster、remind）的測試共用：一個有服飾欄的 CSV 服事表、測試模式發送、固定的「今天」。"""

from __future__ import annotations

import datetime as dt
import json
from zoneinfo import ZoneInfo

from church_bot.cli import build_parser
from church_bot.config import Paths, Settings, save_settings
from church_bot.models import Member, Target
from church_bot.service import BotService
from church_bot.tables import MemberTable, TargetTable
from tests.conftest import gid, write

NOW = dt.datetime(2026, 9, 11, 20, 0, tzinfo=ZoneInfo("Asia/Taipei"))  # 星期五；本週日 9/6，下週日 9/13
ROSTER = """日期,聚會,講員,司琴,招待,服飾
2026/8/30,主日崇拜,李傳道,美華,喜樂,白上衣
2026/9/6,主日崇拜,王牧師,小明,恩典,深色西裝
2026/9/13,主日崇拜,王牧師,小明,喜樂、恩典,深色西裝
2026/9/20,主日崇拜,李傳道,美華,建國,便服
2026/9/27,主日崇拜,王牧師,美華,建國,白上衣
"""


def setup_ministry(paths: Paths, roster: str = ROSTER, *, console: bool = True) -> None:
    write(paths.config_dir / "roster.csv", roster)
    settings = Settings()
    settings.source.kind = "csv"
    settings.source.csv_path = "roster.csv"
    settings.source.columns.text = ["服飾"]
    settings.schedule.enabled = False
    if console:
        settings.messenger.kind = "console"
    save_settings(paths, settings)
    TargetTable(paths.targets_file).save([Target("同工群", gid()), Target("司琴群", gid("c"), roles=("司琴",))])
    MemberTable(paths.members_file).save([Member("王大衛牧師", ("王牧師",)), Member("李傳道"), Member("陳小明", ("小明",)),
                                          Member("林美華", ("美華",))])


def fix_now(monkeypatch) -> None:
    monkeypatch.setattr(BotService, "now", lambda self, settings: NOW)


def run(paths: Paths, *argv: str, capsys) -> tuple[int, dict | list]:
    """跑一個指令（一律加 --json），回傳 (結束碼, 最後一行的 JSON)。"""
    from church_bot.cli import COMMANDS, _api_commands

    args = build_parser().parse_args([*argv, "--json"])
    command = COMMANDS.get(args.command) or _api_commands()[args.command]
    capsys.readouterr()
    code = command(paths, args)
    out = capsys.readouterr().out.strip().splitlines()
    return code, json.loads(out[-1])
