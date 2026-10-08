"""指令列入口：``python -m church_bot <指令>``。

一般使用者不用記這些：雙擊 scripts 資料夾裡的檔案就好。
每週提醒：管理網頁開著的時候，每個牧區照自己的時間自動發（scheduler.py）。
Telegram 機器人可以當備援：每週呼叫 scripts/windows/cli.bat send --retries 3 --retry-wait 300 --popup，
沒指定牧區就發「今天輪到的牧區」；--牧區 青年牧區 只發那一個。
cli.bat 不會問問題、不會停下來等按鍵，跑完就結束，結束代碼 0 = 正常、1 = 有錯誤、2 = 設定有問題。
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING

from church_bot import __version__
from church_bot.config import Paths, load_settings
from church_bot.core import notify, versions
from church_bot.core.planner import Plan
from church_bot.errors import ChurchBotError
from church_bot.logging_setup import setup_logging
from church_bot.models import DeliveryStatus, RunReport, Severity
from church_bot.tables import MemberTable, TargetTable, TeamTable, write_csv

if TYPE_CHECKING:
    from church_bot.ministries import Church, Unit

log = logging.getLogger(__name__)

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
    # flush：被別的程式呼叫（Telegram、排程器）時輸出不是直接到螢幕，不 flush 會整段卡到結束才出現
    try:
        print(text, flush=True)
    except UnicodeEncodeError:  # 極舊的命令列：印不出 emoji 就換成 ?
        print(text.encode(sys.stdout.encoding or "ascii", errors="replace").decode(sys.stdout.encoding or "ascii"),
              flush=True)


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
    """建立 .env 和第一個牧區（範例設定、範例名單、範例服事表）。已經存在的檔案一律不覆蓋。

    ``paths`` 是整個教會：舊版單一牧區先自動搬家；還沒有任何牧區就建「第一個牧區」。
    ``paths`` 指定了牧區就只準備那一個牧區。
    """
    from church_bot.church import add_ministry, load_church, migrate_legacy

    church = paths.church
    church.config_dir.mkdir(parents=True, exist_ok=True)
    church.data_dir.mkdir(parents=True, exist_ok=True)
    created = []
    if not church.env_file.exists():
        shutil.copyfile(church.root / ".env.example", church.env_file)
        created.append(".env")
    target = paths
    if not paths.ministry:
        migrate_legacy(church)
        first = next(iter(load_church(church).ministries), None) or add_ministry(church, "第一個牧區")
        target = church.for_ministry(first.id)
    target.config_dir.mkdir(parents=True, exist_ok=True)
    target.data_dir.mkdir(parents=True, exist_ok=True)
    folder = target.config_dir.relative_to(church.root).as_posix()
    examples = church.config_dir
    blank = _is_blank_new_ministry(target)
    if not target.settings_file.exists() or blank:
        shutil.copyfile(examples / "settings.example.yaml", target.settings_file)
        created.append(f"{folder}/settings.yaml")
    for table_cls, file, example in (
        (TargetTable, target.targets_file, "targets.example.csv"),
        (MemberTable, target.members_file, "members.example.csv"),
        (TeamTable, target.teams_file, "teams.example.csv"),
    ):
        if (not file.exists() or blank) and (examples / example).exists():
            items = table_cls(examples / example).load().items  # 透過讀寫轉成 Excel 看得懂的編碼
            table_cls(file).save(items)
            created.append(f"{folder}/{file.name}")
    demo = target.config_dir / "roster.demo.csv"
    if not demo.exists():
        write_demo_roster(target, dt.date.today())
        created.append(f"{folder}/roster.demo.csv（範例服事表）")
    _print("✅ 設定檔準備好了" + (f"：新增 {', '.join(created)}" if created else "（都已經存在，沒有覆蓋任何檔案）"))
    return 0


def _is_blank_new_ministry(paths: Paths) -> bool:
    """剛用 add_ministry 建好、還沒動過的牧區（表都是空的或還沒建立）：init 可以放心換成範例設定和名單。"""
    for table in (TargetTable(paths.targets_file), MemberTable(paths.members_file), TeamTable(paths.teams_file)):
        try:
            if table.path.exists() and table.load().items:
                return False
        except ChurchBotError:
            return False  # 讀不懂的檔案一定是有人動過，不要蓋掉
    return True


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


def _units(paths: Paths, args: argparse.Namespace, *, due_today: bool = False) -> tuple[Church, list[Unit]]:
    """這次要處理哪些牧區。

    * 指定了牧區（``--牧區 青年牧區``，或 ``paths`` 本身就是某個牧區）→ 只有那一個。
    * ``due_today``（``send`` 沒指定牧區）→ 今天輪到的牧區：自動發送開著、今天是它的發送日、是發送週。
      Telegram 每週固定呼叫一次 ``cli.bat send`` 就會照各牧區自己的發送日發；``--all`` = 全部都發。
    * 其他（``preview``、``check``）→ 全部牧區。
    """
    from church_bot.config import load_settings
    from church_bot.ministries import Church
    from church_bot.scheduler import due_on

    church = Church(paths)
    key = getattr(args, "ministry", "") or paths.ministry
    if key:
        return church, [church.require(key)]
    units = church.units()
    if not units:
        raise ChurchBotError("還沒有任何牧區", "打開管理網頁（2-start）新增第一個牧區，或執行 init。")
    if not due_today or getattr(args, "all", False):
        return church, units
    due = []
    for unit in units:
        try:
            settings = load_settings(unit.service.paths)
        except ChurchBotError as exc:
            _print(f"⚠️ 「{unit.name}」的設定檔有錯，這次跳過：{exc.message}")
            continue
        if due_on(settings.schedule, unit.service.now(settings).date()):
            due.append(unit)
        else:
            _print(f"➖ 「{unit.name}」今天不是發送日（{settings.schedule.describe()}）")
    return church, due


def _heading(unit: Unit, units: list[Unit]) -> None:
    if len(units) > 1 or unit.id != "m1":
        _print(f"\n════════ {unit.name}（{unit.id}）════════")


def cmd_check(paths: Paths, args: argparse.Namespace) -> int:
    _church, units = _units(paths, args)
    _print("教會服事提醒機器人 — 健康檢查")
    worst = 0
    for unit in units:
        _heading(unit, units)
        _print()
        items = unit.service.health()
        for item in items:
            _print(f"{item.icon} {item.name}：{item.detail}")
            if item.hint and item.ok is not True:
                _print(f"   → {item.hint}")
        _print("\n──────── 這次會發的內容（預覽，不會真的送出）────────")
        report, plan = unit.service.preview()
        print_report(report, plan)
        if any(i.ok is False for i in items) or report.has_errors:
            worst = 1
    return worst


def cmd_preview(paths: Paths, args: argparse.Namespace) -> int:
    _church, units = _units(paths, args)
    worst = 0
    for unit in units:
        _heading(unit, units)
        report, plan = unit.service.preview()
        print_report(report, plan)
        worst = max(worst, 1 if report.has_errors else 0)
    return worst


def cmd_send(paths: Paths, args: argparse.Namespace) -> int:
    """發送一次（已經送過、內容沒變的會自動略過）。Telegram 排程就是呼叫這個。

    沒指定牧區 = 今天輪到的牧區（見 _units）。
    --retries：讀不到服事表、LINE 暫時連不上這種「等一下可能就好」的問題，等 --retry-wait 秒再試。
    --popup：最後還是有錯誤，就在這台電腦跳出小視窗通知。
    """
    from church_bot.service import RETRYABLE_CODES, worth_retrying

    _church, units = _units(paths, args, due_today=True)
    if not units:
        _print("今天沒有輪到任何牧區，這次什麼都沒發。")
        return 0
    failed: list[tuple[Unit, RunReport]] = []
    for unit in units:
        _heading(unit, units)
        attempts = max(args.retries, 0) + 1
        for attempt in range(1, attempts + 1):
            left = attempts - attempt
            report, plan = unit.service.run("cli", force=args.force, will_retry=left > 0)
            if left == 0 or not worth_retrying(report):
                break
            reason = next(i.message for i in report.issues if i.is_error and i.code in RETRYABLE_CODES)
            _print(f"⚠️ 第 {attempt} 次沒成功：{reason}")
            _print(f"   {args.retry_wait:g} 秒後再試（還會再試 {left} 次）")
            time.sleep(args.retry_wait)
        print_report(report, plan)
        if report.has_errors:
            failed.append((unit, report))
    if failed and args.popup:
        title = "服事提醒機器人：提醒沒有順利送出"
        text = "\n\n".join((f"【{unit.name}】\n" if len(units) > 1 else "") + _popup_text(report)
                           for unit, report in failed)
        _popup(title, text)
    return 1 if failed else 0


def cmd_quota(paths: Paths, args: argparse.Namespace) -> int:
    """印出本月 LINE 用量，並在該查的時候向 LINE 重新問一次。

    給 Telegram 當「發完之後的回頭確認」用：排在 send 之後約 5 分鐘呼叫一次
    （LINE 的用量統計會延遲幾分鐘，發送當下問到的數字通常還沒算進這一次），
    管理網頁下次打開看到的就是發送後的真實用量。管理網頁開著的話它自己會更新，不用這個指令。
    用量是整個 LINE 帳號一份，所有牧區共用。
    """
    from church_bot.ministries import Church

    snapshot = Church(paths).refresh_quota(force=args.force)
    if snapshot is None:
        _print("➖ 目前不檢查 LINE 額度（每個牧區都是測試模式，或設定裡關掉了額度檢查）")
        return 0
    _print(f"📊 {snapshot.describe()}")
    _print(f"   {snapshot.status_text(dt.datetime.now().astimezone())}")
    return 1 if snapshot.error else 0


def cmd_ministries(paths: Paths, args: argparse.Namespace) -> int:
    """牧區清單：列出、新增、改名、清掉第二層密碼（忘記密碼時用，只有在這台電腦上才能做）。"""
    from church_bot.church import add_ministry, edit_ministry
    from church_bot.config import load_settings
    from church_bot.ministries import Church

    church = Church(paths)
    action = getattr(args, "action", "") or "list"
    if action == "add":
        ministry = add_ministry(church.paths, args.name, getattr(args, "note", "") or "")
        _print(f"✅ 已新增「{ministry.name}」（編號 {ministry.id}）。打開管理網頁就看得到。")
        return 0
    if action == "rename":
        unit = church.require(args.ministry)
        renamed = edit_ministry(church.paths, unit.id, name=args.name)
        _print(f"✅ 「{unit.name}」改名成「{renamed.name}」（編號不變：{unit.id}）")
        return 0
    if action == "temp-password":
        from church_bot.church import MIN_MINISTRY_PASSWORD, hash_password
        from church_bot.config import read_env_file

        unit = church.require(args.ministry)
        site = os.environ.get("UI_PASSWORD") or read_env_file(church.paths.env_file).get("UI_PASSWORD", "")
        password = (args.password or site).strip()
        if len(password) < MIN_MINISTRY_PASSWORD:
            raise ChurchBotError("沒有可以用的臨時密碼", "網站沒設密碼（.env 的 UI_PASSWORD）；請用 --password 指定一組。")
        edit_ministry(church.paths, unit.id, password_hash=hash_password(password), temporary=True)
        which = "自己指定的那一組" if args.password else "跟網站密碼一樣"
        _print(f"✅ 已給「{unit.name}」設臨時密碼（{which}）。那個牧區的人第一次用它進來，要先換成自己的。")
        return 0
    if action == "clear-password":
        unit = church.require(args.ministry)
        edit_ministry(church.paths, unit.id, password_hash="")
        _print(f"✅ 已清掉「{unit.name}」的牧區密碼，現在只要網站密碼就進得去。")
        return 0
    units = church.units()
    if not units:
        _print("還沒有任何牧區。打開管理網頁（2-start）新增第一個，或執行 init。")
        return 0
    for unit in units:
        try:
            schedule = load_settings(unit.service.paths).schedule.describe()
        except ChurchBotError as exc:
            schedule = f"設定檔有錯：{exc.message}"
        lock = "🔒 " if unit.ministry.has_password else ""
        groups = sum(1 for t in unit.targets() if t.enabled)
        _print(f"{unit.id}\t{lock}{unit.name}\t{groups} 個群組會收到提醒\t{schedule}")
    _print("\nTelegram 可以呼叫：cli.bat send（今天輪到的牧區）、cli.bat send --牧區 名稱（只發那一個）")
    return 0


def cmd_status(paths: Paths, args: argparse.Namespace) -> int:
    """所有牧區現在的狀況：服事表排到哪、上一次發了什麼、下一次什麼時候發。

    給伺服器管理員用的那一個指令。``--telegram`` 會把同一份傳到手機，人不在電腦前也看得到。
    結束代碼：0 = 都正常、1 = 有牧區要處理（服事表快排完、讀不到、上一次發送有錯誤）。
    """
    from church_bot import status as status_mod
    from church_bot.ministries import Church

    church = Church(paths)
    statuses = status_mod.collect(church, check_roster=not args.quick)
    text = status_mod.text(church, statuses, detail=not args.short)
    _print(text)
    if args.telegram:
        ok = notify.Notifier(paths).send(text)
        _print("\n（已傳到 Telegram）" if ok else
               "\n（Telegram 還沒設定好、或這次傳不出去，所以只印在這裡）")
    return 1 if any(not s.ok for s in statuses) else 0


def _popup_text(report: RunReport, limit: int = 5) -> str:
    problems = [i for i in report.issues if i.is_error]
    lines = [f"已送出 {report.count_sent} 則、失敗 {report.count_failed} 則。", ""]
    lines += [i.one_line() for i in problems[:limit]]
    if len(problems) > limit:
        lines.append(f"…還有 {len(problems) - limit} 項")
    lines += ["", "詳細請打開管理網頁（2-start），或看 data/church_bot.log。"]
    return "\n".join(lines)


def _popup(title: str, text: str) -> None:
    """在這台電腦跳出小視窗。另開一個程式顯示，呼叫的人（Telegram）不用等使用者按確定就能結束。"""
    if sys.platform != "win32":
        return
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    code = "import ctypes, sys; ctypes.windll.user32.MessageBoxW(0, sys.argv[1], sys.argv[2], 0x30 | 0x10000 | 0x40000)"
    try:
        subprocess.Popen([str(pythonw if pythonw.exists() else sys.executable), "-c", code, text, title],
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP, close_fds=True)
    except OSError as exc:
        _print(f"（跳不出通知視窗：{exc}）")


# --------------------------------------------------------------------------- LINE Webhook（免費模式）


def cmd_set_webhook(paths: Paths, args: argparse.Namespace) -> int:
    """把網址登記成 LINE 的 Webhook URL（免費模式會自動做；這個指令給自己架固定網址的人用）。"""
    from church_bot.tunnel import register_webhook

    if not args.url.strip().startswith("https://"):
        _print("❌ Webhook 網址一定要是 https:// 開頭")
        return 2
    return 0 if register_webhook(paths, args.url, _print, tries=args.tries, wait=args.wait) else 1


def cmd_tunnel(paths: Paths, args: argparse.Namespace) -> int:
    """免費模式：開 Cloudflare 臨時網址 + 自動登記到 LINE，一直開著直到關掉視窗或 Ctrl+C（見 tunnel.py）。"""
    from church_bot.tunnel import register_webhook, run_tunnel

    command = [args.cloudflared, "tunnel", "--url", f"http://localhost:{args.port}"]
    return run_tunnel(command, lambda url: register_webhook(paths, url, _print), _print)


# --------------------------------------------------------------------------- web


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return True
    return False


def _port_owner(port: int) -> str:
    """誰在用這個 port（只有 Windows 查得到；查不到就空字串）。給「已經有程式在用了」那一句話用。"""
    if sys.platform != "win32":
        return ""
    def run(*command: str) -> str:  # 中文 Windows 的 netstat／tasklist 是 cp950；2-start 又開了 UTF-8 模式，所以自己解碼
        out = subprocess.run(command, capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW).stdout
        return out.decode("mbcs", errors="replace")

    try:
        pid = next((line.split()[-1] for line in run("netstat", "-ano", "-p", "TCP").splitlines()
                    if len(line.split()) >= 5 and line.split()[1].endswith(f":{port}") and line.split()[-1] != "0"
                    and line.split()[2].endswith((":0", ":*"))), "")
        if not pid:
            return ""
        task = run("tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH")
    except (OSError, subprocess.SubprocessError, IndexError):
        return ""
    name = task.strip().split(",")[0].strip('"') if task.strip().startswith('"') else ""
    return f"{name or '某個程式'}（PID {pid}）"


SERVICE_NAME = "church-bot"  # 註冊成 Windows 服務時的名字（見 scripts/windows/service/service.ps1）


def _service_running() -> bool:
    """後台是不是以 Windows 服務的身分在跑。查詢服務狀態不用系統管理員權限。"""
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(["sc", "query", SERVICE_NAME], capture_output=True, timeout=10,
                             creationflags=subprocess.CREATE_NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return b"RUNNING" in out


PORT_IN_USE = 4  # 已經有程式在用這個 port：讓 2-start 停下來把原因顯示出來，不要一閃就關掉
RESTART_CODE = 3  # 管理網頁按「重新啟動」：網頁那個子程式用這個代碼結束，外面那一層就再開一次
RESTART_LIMIT = 5  # 一分鐘內重開超過這麼多次就停下來（一直壞的話不要無限重開）


def cmd_web(paths: Paths, args: argparse.Namespace) -> int:
    """開管理網頁。外面這一層只負責「開網頁、網頁說要重新啟動就再開一次」，真正的網頁在子程式裡跑
    （``--child``），所以按「重新啟動」會載入新的程式，例如更新過程式之後。2-start、免費模式、開機自動執行都一樣。"""
    if not getattr(args, "child", False):
        return _supervise(paths, args)
    return _serve(paths, args)


def _supervise(paths: Paths, args: argparse.Namespace) -> int:
    base = [sys.executable, "-m", "church_bot", *(["-v"] if getattr(args, "verbose", False) else []), "web", "--child"]
    base += ["--host", args.host] if args.host else []
    base += ["--port", str(args.port)] if args.port else []
    starts: list[float] = []
    restarted = False
    while True:
        command = base + (["--no-browser"] if args.no_browser or restarted else []) + (["--restarted"] if restarted else [])
        child = subprocess.Popen(command)
        try:
            code = child.wait()
        except KeyboardInterrupt:  # Ctrl+C 也會送到子程式，等它把排程收好再結束
            try:
                child.wait(timeout=30)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                child.kill()
            return 0  # 「已停止」那一則由子程式自己傳（它才知道有沒有收好），這裡不重複
        if code != RESTART_CODE:
            # 子程式自己結束得掉的話（正常關閉、按重新啟動）都不會走到這裡還帶著錯誤代碼。
            # 走到這裡 = 它死掉了，而且死得太突然，來不及自己講——這一層是唯一還活著的人。
            if code != 0:
                _notify_crash(paths, code)
            return code
        now = time.monotonic()
        starts = [t for t in starts if now - t < 60] + [now]
        if len(starts) > RESTART_LIMIT:
            _print("❌ 一分鐘內重新啟動太多次，先停下來。請把 data/church_bot.log 傳給維護的人，再雙擊 2-start。")
            notify.Notifier(paths).send("❌ 服事提醒機器人：後台一分鐘內重新啟動太多次，已經停下來不再重開。\n"
                                        "請到那台電腦看 data/church_bot.log。", wait=notify.STOP_WAIT)
            return 1
        _print("🔄 重新啟動管理網頁…")
        restarted = True


def _notify_crash(paths: Paths, code: int) -> None:
    if code == PORT_IN_USE:
        text = ("⚠️ 服事提醒機器人：後台沒有啟動。\n"
                "網址的 port 已經被別的程式佔用了（很可能本來就有一個開著）。")
    else:
        text = (f"❌ 服事提醒機器人：後台意外停止（結束代碼 {code}）。\n"
                "自動發送在它重新起來之前都不會動。請到那台電腦看 data/church_bot.log。")
    notify.Notifier(paths).send(text, wait=notify.STOP_WAIT)  # 不能拖太久：服務要等這裡結束才重開


def _serve(paths: Paths, args: argparse.Namespace) -> int:
    import uvicorn

    from church_bot.web.app import create_app

    settings = load_settings(paths)
    host = args.host or settings.web.host
    port = args.port or settings.web.port
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}"
    if args.restarted:  # 舊的那個剛結束，port 可能還要一下下才放出來
        for _ in range(40):
            if not _port_in_use(host, port):
                break
            time.sleep(0.5)
    if _port_in_use(host, port):
        owner = _port_owner(port)
        _print(f"⚠️ {url} 已經有程式在用了{('：' + owner) if owner else ''}")
        _print("   很可能是管理網頁本來就開著——免費模式（3-open-webhook）也會順便開一個。已經幫你打開瀏覽器。")
        if _service_running():
            _print("   這台電腦已經把後台註冊成 Windows 服務了，所以它開機就一直在跑，不用再雙擊 2-start。")
            _print("   要停它請用 scripts\\windows\\service\\stop.bat，更新過程式用 restart.bat。")
        else:
            _print("   想換成這一次開的（例如更新過程式）：先關掉原本那個視窗，再雙擊一次 2-start；")
            _print("   或在網頁右上角按「重新啟動」（伺服器管理員）。")
        if not args.no_browser:
            webbrowser.open(url)
        return PORT_IN_USE  # 不是 0：2-start 會停下來（按任意鍵才關），不會一閃就不見
    if host not in ("127.0.0.1", "localhost") and not settings.web.password:
        _print("⚠️ 管理網頁開放給其他電腦連線，但沒有設定密碼！請在 .env 設定 UI_PASSWORD。")

    if args.restarted:
        _print(f"✅ 已重新啟動：{url}")
    else:
        _print(f"✅ 管理網頁：{url}")
        _print("   要關掉請按 Ctrl + C。")
        _print("   這個視窗開著的時候，「自動發送」開著的牧區會照各自的時間發（Telegram 再呼叫一次也不會發兩次）。")
    if not args.no_browser:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    app = create_app(paths)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    app.state.server = server  # 「重新啟動」按鈕靠它讓網頁停下來（見 web/app.py）

    church = app.state.web.church
    notifier = notify.Notifier(paths)
    before = notify.record_start(church.shared)
    # 另開執行緒去傳：Telegram 連不上時最多要等 10 秒，不能讓網頁晚 10 秒才開
    threading.Thread(target=_announce_start, args=(notifier, church, url, before, args.restarted),
                     daemon=True).start()

    server.run()

    restarting = bool(getattr(app.state, "restart_requested", False))
    notify.record_stop(church.shared, clean=True, reason="重新啟動" if restarting else "正常關閉")
    if restarting:
        notifier.send("🔄 服事提醒機器人：後台正在重新啟動…", wait=notify.STOP_WAIT)
    else:
        notifier.send("⏹ 服事提醒機器人：後台已經正常關閉。\n"
                      "在它重新開起來之前，每週提醒不會自動發送。", wait=notify.STOP_WAIT)
    return RESTART_CODE if restarting else 0


def _announce_start(notifier: "notify.Notifier", church: "Church", url: str, before: dict, restarted: bool) -> None:
    """後台開起來了。順便講「上一次有沒有正常關閉」——伺服器自己不見的時候，就是靠這一行發現的。"""
    from church_bot import status as status_mod

    notifier.flush()  # Notifier 沒開著時積下來的，趁現在補送（只在啟動這一刻做一次，不是輪詢）
    head = "🔄 服事提醒機器人：後台已重新啟動" if restarted else "✅ 服事提醒機器人：後台已啟動"
    lines = [f"{head}（v{__version__}）", f"管理網頁：{url}"]
    if os.environ.get("CHURCH_BOT_SERVICE"):
        lines.append("（以 Windows 服務執行：關機重開、當掉都會自己再起來）")
    if warning := notify.previous_shutdown_line(before):
        lines += ["", warning]
    try:  # check_roster=False：啟動通知要快，不連網去讀 Google 服事表
        lines += ["", *status_mod.lines(status_mod.collect(church, check_roster=False), detail=False)]
    except Exception:  # noqa: BLE001 - 通知裡少一段，絕對不能害後台開不起來
        log.exception("組啟動通知的牧區狀況時失敗")
    notifier.send("\n".join(lines))


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
    web.add_argument("--child", action="store_true", help=argparse.SUPPRESS)  # 外面那一層開的子程式（見 cmd_web）
    web.add_argument("--restarted", action="store_true", help=argparse.SUPPRESS)
    ministry_help = "只處理這個牧區（名稱或編號，例如 青年牧區 或 m1）"
    check = sub.add_parser("check", help="健康檢查：設定、服事表、LINE 是否都正常（預設每個牧區）")
    check.add_argument("--牧區", "--ministry", "-m", dest="ministry", default="", help=ministry_help)
    preview = sub.add_parser("preview", help="預覽這次會發的訊息（不會真的送出；預設每個牧區）")
    preview.add_argument("--牧區", "--ministry", "-m", dest="ministry", default="", help=ministry_help)
    send = sub.add_parser("send", help="立刻發送一次（預設：今天輪到的牧區）")
    send.add_argument("--牧區", "--ministry", "-m", dest="ministry", default="", help=ministry_help)
    send.add_argument("--all", action="store_true", help="不管今天是不是發送日，每個牧區都發")
    send.add_argument("--force", action="store_true", help="已經送過的也再送一次")
    send.add_argument("--retries", type=int, default=0, help="讀不到服事表、LINE 暫時連不上時，最多再試幾次（預設 0）")
    send.add_argument("--retry-wait", type=float, default=300, help="每次重試前等幾秒（預設 300 = 5 分鐘）")
    send.add_argument("--popup", action="store_true", help="最後還是失敗的話，在這台電腦跳出小視窗通知")
    state = sub.add_parser("狀態", aliases=["status"], help="所有牧區的狀況：服事表排到哪、上次發送、下次發送")
    state.add_argument("--telegram", action="store_true", help="同時傳一份到伺服器管理員的 Telegram")
    state.add_argument("--quick", action="store_true", help="不去讀服事表（不連網，瞬間就好）")
    state.add_argument("--short", action="store_true", help="一個牧區只印一行")
    quota = sub.add_parser("quota", help="查本月 LINE 用量（發送後約 5 分鐘呼叫一次，用量就會是發送後的數字）")
    quota.add_argument("--force", action="store_true", help="不管上次查多久以前，一定重新問 LINE")
    ministries = sub.add_parser("牧區", aliases=["ministries"], help="牧區清單：列出、新增、改名、清掉牧區密碼")
    actions = ministries.add_subparsers(dest="action", metavar="動作")
    actions.add_parser("list", help="列出所有牧區（預設）")
    add = actions.add_parser("add", help="新增牧區")
    add.add_argument("name", help="牧區名稱")
    add.add_argument("--note", default="", help="備註，例如負責人")
    rename = actions.add_parser("rename", help="改名（編號不變）")
    rename.add_argument("ministry", help="現在的名稱或編號")
    rename.add_argument("name", help="新的名稱")
    temp = actions.add_parser("temp-password", help="給牧區一組臨時密碼（預設 = 網站密碼），第一次用它進來要換成自己的")
    temp.add_argument("ministry", help="名稱或編號")
    temp.add_argument("--password", default="", help="自己指定臨時密碼（不給 = 用網站密碼）")
    clear = actions.add_parser("clear-password", help="清掉牧區密碼（忘記密碼時，在這台電腦上執行）")
    clear.add_argument("ministry", help="名稱或編號")
    hook = sub.add_parser("set-webhook", help="把臨時網址登記成 LINE 的 Webhook URL，並請 LINE 測試連線")
    hook.add_argument("url", help="https:// 開頭的網址（沒加 /line/webhook 會自動補上）")
    hook.add_argument("--tries", type=int, default=12, help=argparse.SUPPRESS)
    hook.add_argument("--wait", type=float, default=5.0, help=argparse.SUPPRESS)
    tunnel = sub.add_parser("tunnel", help="免費模式：開臨時網址並自動登記到 LINE（開著的時候群組指令才會回）")
    tunnel.add_argument("--port", type=int, default=8787, help="管理網頁的 port")
    tunnel.add_argument("--cloudflared", default="cloudflared", help="cloudflared 的位置")
    return parser


COMMANDS = {"init": cmd_init, "web": cmd_web, "check": cmd_check, "preview": cmd_preview, "send": cmd_send,
            "quota": cmd_quota, "牧區": cmd_ministries, "ministries": cmd_ministries, "set-webhook": cmd_set_webhook,
            "tunnel": cmd_tunnel, "狀態": cmd_status, "status": cmd_status}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = Paths.discover()
    setup_logging(paths, verbose=args.verbose)
    command = COMMANDS.get(args.command or "web")
    try:
        with versions.source("指令列"):
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
