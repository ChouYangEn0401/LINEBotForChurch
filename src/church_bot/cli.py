"""指令列入口：``python -m church_bot <指令>``。

一般使用者不用記這些：雙擊 scripts 資料夾裡的檔案就好。
"""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
import socket
import sys
import threading
import webbrowser

from church_bot import __version__
from church_bot.config import Paths, load_settings
from church_bot.core.planner import Plan
from church_bot.errors import ChurchBotError
from church_bot.logging_setup import setup_logging
from church_bot.models import DeliveryStatus, RunReport, Severity
from church_bot.tables import MemberTable, TargetTable, write_csv

DEMO_HEADER = ["日期", "聚會", "講員", "司會", "敬拜主領", "司琴", "音控", "投影", "招待", "備註"]
DEMO_ROWS = [
    ["王牧師", "建國", "小明", "美華", "阿豪", "心怡", "蔡弟兄、恩典", ""],
    ["李傳道", "喜樂", "美華", "小明(代)", "阿豪", "Ming", "建國、許心怡", "聖餐主日"],
    ["王牧師", "恩典", "小明", "美華", "阿德哥", "心怡", "喜樂、恩典", ""],  # 阿德哥 → 同工名單找不到（示範警告）
    ["大衛牧師", "建國", "以琳", "美華", "阿豪", "-", "蔡弟兄、心怡", ""],  # 以琳 → 停用（示範警告）
    ["李傳道", "喜樂", "小明", "美華", "阿豪", "心怡", "恩典、建國", "宣教主日"],
    ["王牧師", "恩典", "美華 小明", "林美華", "張志豪", "許心怡", "喜樂", ""],
    ["王牧師", "建國", "小明", "美華", "阿豪", "心怡", "蔡弟兄、恩典", ""],
    ["李傳道", "喜樂", "美華", "小明", "阿豪", "Ming", "建國、許心怡", ""],
]


def _print(text: str = "") -> None:
    try:
        print(text)
    except UnicodeEncodeError:  # 極舊的命令列：印不出 emoji 就換成 ?
        print(text.encode(sys.stdout.encoding or "ascii", errors="replace").decode(sys.stdout.encoding or "ascii"))


# --------------------------------------------------------------------------- init


def write_demo_roster(paths: Paths, today: dt.date) -> None:
    last_sunday = today - dt.timedelta(days=(today.weekday() + 1) % 7)
    start = last_sunday - dt.timedelta(days=7)
    rows = []
    for i, names in enumerate(DEMO_ROWS):
        d = start + dt.timedelta(days=7 * i)
        rows.append([f"{d.year}/{d.month}/{d.day}", "主日崇拜", *names])
    write_csv(paths.config_dir / "roster.demo.csv", DEMO_HEADER, rows)


def cmd_init(paths: Paths, _: argparse.Namespace) -> int:
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    created = []
    if not paths.env_file.exists():
        shutil.copyfile(paths.root / ".env.example", paths.env_file)
        created.append(".env")
    if not paths.settings_file.exists():
        shutil.copyfile(paths.config_dir / "settings.example.yaml", paths.settings_file)
        created.append("config/settings.yaml")
    for table_cls, target, example in (
        (TargetTable, paths.targets_file, "targets.example.csv"),
        (MemberTable, paths.members_file, "members.example.csv"),
    ):
        if not target.exists():
            items = table_cls(paths.config_dir / example).load().items  # 透過讀寫轉成 Excel 看得懂的編碼
            table_cls(target).save(items)
            created.append(f"config/{target.name}")
    demo = paths.config_dir / "roster.demo.csv"
    if not demo.exists():
        write_demo_roster(paths, dt.date.today())
        created.append("config/roster.demo.csv（範例服事表）")
    _print("✅ 設定檔準備好了" + (f"：新增 {', '.join(created)}" if created else "（都已經存在，沒有覆蓋任何檔案）"))
    return 0


# --------------------------------------------------------------------------- reporting


def print_report(report: RunReport, plan: Plan | None = None, show_messages: bool = True) -> None:
    _print(f"\n結果：{report.status_zh}")
    if show_messages:
        for d in report.deliveries:
            _print(f"\n──── {d.status.zh}｜{d.target_name} ────")
            if d.detail:
                _print(f"（{d.detail}）")
            if d.status in (DeliveryStatus.DRY_RUN, DeliveryStatus.SENT):
                _print(d.text)
    if not report.deliveries:
        if plan is not None and plan.samples:
            _print()
            _print("（還沒有可以收到提醒的群組。訊息會長這樣：）")
            for _day, sample in plan.samples:
                _print()
                _print(sample.text)
        else:
            _print("（這次沒有要發的訊息）")
    problems = [i for i in report.issues if i.severity is not Severity.INFO]
    infos = [i for i in report.issues if i.severity is Severity.INFO]
    if problems:
        _print("\n需要處理的事項：")
        for issue in problems:
            _print(issue.one_line())
    if infos:
        _print("\n參考資訊：")
        for issue in infos:
            _print(issue.one_line())


def cmd_check(paths: Paths, _: argparse.Namespace) -> int:
    from church_bot.service import BotService

    service = BotService(paths)
    _print("教會服事提醒機器人 — 健康檢查\n")
    items = service.health()
    for item in items:
        _print(f"{item.icon} {item.name}：{item.detail}")
        if item.hint and item.ok is not True:
            _print(f"   → {item.hint}")
    _print("\n──────── 這次會發的內容（預覽，不會真的送出）────────")
    report, plan = service.preview()
    print_report(report, plan)
    return 1 if any(i.ok is False for i in items) or report.has_errors else 0


def cmd_preview(paths: Paths, _: argparse.Namespace) -> int:
    from church_bot.service import BotService

    report, plan = BotService(paths).preview()
    print_report(report, plan)
    return 1 if report.has_errors else 0


def cmd_send(paths: Paths, args: argparse.Namespace) -> int:
    from church_bot.service import BotService

    report, plan = BotService(paths).run("cli", force=args.force)
    print_report(report, plan)
    return 1 if report.has_errors else 0


# --------------------------------------------------------------------------- web


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return True
    return False


def cmd_web(paths: Paths, args: argparse.Namespace) -> int:
    import uvicorn

    from church_bot.web.app import create_app

    settings = load_settings(paths)
    host = args.host or settings.web.host
    port = args.port or settings.web.port
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}"
    if _port_in_use(host, port):
        _print(f"⚠️ {url} 已經有程式在用了 —— 很可能管理網頁本來就開著。")
        _print("   直接幫你打開瀏覽器；如果打不開，請把設定裡的 port 改成別的數字（例如 8788）。")
        if not args.no_browser:
            webbrowser.open(url)
        return 0
    if host not in ("127.0.0.1", "localhost") and not settings.web.password:
        _print("⚠️ 管理網頁開放給其他電腦連線，但沒有設定密碼！請在 .env 設定 UI_PASSWORD。")

    _print(f"✅ 管理網頁：{url}")
    _print("   這個視窗要一直開著，自動提醒才會運作。要關掉請按 Ctrl + C。")
    if not args.no_browser:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    uvicorn.run(create_app(paths), host=host, port=port, log_level="warning")
    return 0


# --------------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="church-bot", description="教會服事表 LINE 自動提醒機器人")
    parser.add_argument("-v", "--verbose", action="store_true", help="顯示更詳細的紀錄")
    parser.add_argument("--version", action="version", version=f"church-bot {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="指令")

    sub.add_parser("init", help="建立設定檔與範例資料（已存在的不會覆蓋）")
    web = sub.add_parser("web", help="開啟管理網頁 + 自動排程（平常就用這個）")
    web.add_argument("--host", help="覆蓋設定檔的 host")
    web.add_argument("--port", type=int, help="覆蓋設定檔的 port")
    web.add_argument("--no-browser", action="store_true", help="不要自動打開瀏覽器")
    sub.add_parser("check", help="健康檢查：設定、服事表、LINE 是否都正常")
    sub.add_parser("preview", help="預覽這次會發的訊息（不會真的送出）")
    send = sub.add_parser("send", help="立刻發送一次")
    send.add_argument("--force", action="store_true", help="已經送過的也再送一次")
    return parser


COMMANDS = {"init": cmd_init, "web": cmd_web, "check": cmd_check, "preview": cmd_preview, "send": cmd_send}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = Paths.discover()
    setup_logging(paths, verbose=args.verbose)
    command = COMMANDS.get(args.command or "web")
    try:
        return command(paths, args)  # type: ignore[misc]
    except ChurchBotError as exc:
        _print(f"❌ {exc.message}")
        if exc.hint:
            _print(f"   → {exc.hint}")
        return 2
    except KeyboardInterrupt:
        _print("\n已停止。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
