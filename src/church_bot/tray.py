"""右下角的小圖示：後台（L）、LINE webhook（Lw）各一個。

擁有者 2026-10-09：「服務在背景運作的時候，我要能在小工具裡面看到它在運作，然後可以透過小工具去暫停服務」，
而且「不管用何種方式運作，都要讓 2 個程式顯示小圖示在下面」。選單：

    開起來／結束程式（服務模式下 nssm 會再把它開起來）／服務：停止／服務：啟動／服務：重新啟動
    ／安裝成服務（還沒註冊時）／打開紀錄檔

**圖示是小主管程式，不會自己不見**（擁有者 2026-10-09：「小圖示不可以不見，不然我要如何開回來？」）：
選單沒有「關掉這個小圖示」，服務停了圖示照樣在、變灰。取消註冊不放選單（要去資料夾雙擊 uninstall.bat，避免誤觸）。

**名字**照擁有者定的家族慣例（以後別的專案也一樣）：圖示上是「專案字母＋元件記號」，這個專案是 L、Lw；
提示文字以「專案 · 元件」開頭（LINE · 後台、LINE · webhook）。ClawBot 是 C／Ct。
共同規則寫在 MyFirstTelegramClawBot 的 docs/tray-standard.html。

**狀態和能按什麼來自同一個判斷**（``describe`` → ``Status`` → ``menu_state``），而且**任何狀態都至少有一個能按的**——
2026-10-09 擁有者看到「服務開著、小圖示說沒在跑、又按不了啟動」，那個圖示等於沒用。
顏色：綠＝服務在跑；藍＝雙擊開的在跑；黃＝服務開著但程式沒回應、或正在啟動／停止；灰＝沒在跑。

**程式在不在**：服務開著時看 nssm 底下有沒有子程式（``runctl.service_app_alive``，不靠程式自己寫的紀錄，
所以舊版程式也認得）；雙擊開的看 ``data/run/*.json``。

**多久看一次**：每 5 秒在這個程式裡面問一次 Windows（服務控制器＋程式清單），不開新程式、不連網。
擁有者否決了「點開選單才查」：那樣它掛了也看不到。從「在跑」變成「不在」而且不是你按的，會跳一個通知。

開法：``scripts\\windows\\tray.pyw``（pythonw，沒有黑色視窗）。不帶參數＝兩個都開（已經開著的不重開）；
帶 ``web`` 或 ``webhook``＝只跑那一個。登入 Windows 時由「啟動」資料夾的捷徑開（service\\install.bat 放的），
雙擊 2-start／3-open-webhook 時也會順手開。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path

from church_bot import runctl
from church_bot.config import Paths

log = logging.getLogger(__name__)

REFRESH_SECONDS = 5.0
STUCK_SECONDS = 60  # 「正在啟動／停止」超過這麼久＝卡住了，把停止、重新啟動還給你
COLORS = {
    "service": (46, 160, 67),    # 綠
    "manual": (31, 111, 235),    # 藍
    "busy": (210, 153, 34),      # 黃
    "down": (140, 140, 140),     # 灰
}
GLYPHS = {"web": "L", "webhook": "Lw"}  # 專案字母＋元件記號（家族慣例）
PENDING = {"start_pending": "正在啟動", "stop_pending": "正在停止",
           "continue_pending": "正在繼續", "pause_pending": "正在暫停"}
ACTION_ZH = {"stop": "停止", "start": "啟動", "restart": "重新啟動"}
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class Status:
    color: str           # COLORS 的鍵
    line: str            # 滑鼠停在上面、選單第一行看到的那一句
    alive: bool          # 程式本身在跑
    service: str | None  # 服務狀態；None＝沒註冊成服務
    stuck: bool = False  # 卡在「正在啟動／停止」太久


def describe(program: runctl.Program, service: str | None, run: dict | None, *,
             service_app: bool = False, stuck: bool = False) -> Status:
    """把「服務狀態」＋「程式在不在」翻成一個顏色和一句話。純函式，測試直接餵。

    run：data/run/*.json 的紀錄（雙擊開的、新版服務）；service_app：nssm 底下真的有程式。
    """
    title = program.title
    if service == "running" and (service_app or (run and run.get("mode") == "service")):
        return Status("service", f"{title}：執行中（Windows 服務）", True, service)
    if run and run.get("mode") != "service":
        return Status("manual", f"{title}：執行中（{program.launcher} 開的）", True, service)
    if service in PENDING:
        if stuck:
            return Status("busy", f"{title}：服務卡在「{PENDING[service]}」超過 1 分鐘——可以按「服務：重新啟動」或「停止」",
                          False, service, stuck=True)
        return Status("busy", f"{title}：服務{PENDING[service]}…", False, service)
    if service == "running":
        return Status("busy", f"{title}：服務在跑，但程式沒回應（可能正在重開）——可以按「服務：重新啟動」",
                      False, service)
    if service == "paused":
        return Status("busy", f"{title}：服務已暫停——可以按「服務：啟動」", False, service)
    if service == "stopped":
        return Status("down", f"{title}：沒在跑（服務已停止）——可以按「服務：啟動」或「開起來」", False, service)
    return Status("down", f"{title}：沒在跑（還沒註冊成服務）——可以按「開起來」", False, service)


def menu_state(status: Status) -> dict[str, bool]:
    """每個選項現在能不能按。跟 describe 用同一份判斷。

    任何狀態至少有一個 True（測試會檢查）；唯一的例外是「正在啟動／停止」的頭 1 分鐘，
    那幾秒先灰掉免得連按，超過 1 分鐘（卡住）就把停止、重新啟動還給你。
    """
    installed = status.service is not None
    settling = status.service in PENDING and not status.stuck
    return {
        "end": status.alive,
        "stop": installed and status.service != "stopped" and not settling,
        # 雙擊開的那一份還開著時不給啟動：服務會跟它搶 port，起不來又被 nssm 一直重開
        "start": installed and status.service in ("stopped", "paused") and not status.alive,
        "restart": installed and not settling,
        # 沒在跑、服務也沒開著（沒註冊，或註冊了但停著）：用雙擊的方式（2-start／3-open-webhook）開起來。
        # 擁有者 2026-10-09：「如果服務沒開我可以自己手動啟動」——服務停著時也要給，不是只有沒註冊才給
        "launch": not status.alive and status.service in (None, "stopped"),
        # 還沒註冊成服務：選單裡可以直接裝（nssm install；會跳 UAC）。取消註冊刻意不放選單（要去資料夾雙擊，避免誤觸）
        "install": not installed,
    }


# --------------------------------------------------------------------------- 服務控制


def _nssm(paths: Paths) -> Path:
    return paths.church.root / "tools" / "nssm.exe"


def _denied(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in ("access is denied", "拒絕存取", "存取被拒", "error 5", "錯誤 5"))


def control_service(paths: Paths, program: runctl.Program, action: str) -> tuple[bool, str]:
    """nssm start / stop / restart。沒有權限（install.bat 沒授權過）就改用系統管理員身分再做一次（會跳 UAC）。"""
    nssm = _nssm(paths)
    if not nssm.exists():
        return False, "找不到 tools\\nssm.exe：還沒註冊成服務（雙擊 scripts\\windows\\service\\install.bat）"
    try:
        done = subprocess.run([str(nssm), action, program.service], capture_output=True, timeout=120,
                              creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"nssm 執行失敗：{exc}"
    text = (done.stdout + done.stderr).decode("mbcs", errors="replace").replace("\x00", "").strip()
    if done.returncode == 0:
        return True, text
    if _denied(text) and sys.platform == "win32":
        import ctypes

        # 0 = SW_HIDE：UAC 那一下之後不會再多一個黑色視窗
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", str(nssm), f"{action} {program.service}", None, 0)
        if rc > 32:
            return True, "已經用系統管理員身分送出（按了「是」才會生效）"
        return False, "沒有取得系統管理員權限"
    return False, text or f"nssm {action} 失敗（代碼 {done.returncode}）"


# --------------------------------------------------------------------------- 圖示


def _font(size: int):
    from PIL import ImageFont

    for name in ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render(program: runctl.Program, color: str, size: int = 64):
    """圓角方塊＋白字：「L」＝後台、「Lw」＝webhook。顏色就是狀態。字縮到放得進方塊為止。"""
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, size - 3, size - 3), radius=size // 5, fill=COLORS[color] + (255,))
    glyph = GLYPHS[program.key]
    font_size = int(size * 0.7)
    while True:
        font = _font(font_size)
        left, top, right, bottom = draw.textbbox((0, 0), glyph, font=font)
        if right - left <= size * 0.8 or font_size <= 8:
            break
        font_size -= 2
    draw.text((size / 2, size / 2), glyph, fill=(255, 255, 255, 255), font=font, anchor="mm")
    return image


# --------------------------------------------------------------------------- 小圖示本體


class Tray:
    def __init__(self, paths: Paths, program: runctl.Program) -> None:
        self.paths = paths.church
        self.program = program
        self._pending_since: float | None = None
        self.status = self.snapshot()
        self.icon = None
        self._stop = threading.Event()
        self._refresh_now = threading.Event()
        self._expect_down = False  # 是你按的（結束、停止、重開）就不要跳「停了」的通知

    def snapshot(self) -> Status:
        service = runctl.service_state(self.program)
        if service in PENDING:
            self._pending_since = self._pending_since or time.monotonic()
        else:
            self._pending_since = None
        stuck = self._pending_since is not None and time.monotonic() - self._pending_since > STUCK_SECONDS
        return describe(self.program, service, runctl.running(self.paths, self.program),
                        service_app=service == "running" and runctl.service_app_alive(self.program), stuck=stuck)

    # -- 選單

    def _menu(self):
        import pystray
        from pystray import Menu, MenuItem as Item

        can = lambda key: (lambda item: menu_state(self.status)[key])  # noqa: E731
        under_service = lambda: self.status.alive and self.status.service == "running"  # noqa: E731
        items = [
            Item(lambda item: self.status.line, None, enabled=False),
            Menu.SEPARATOR,
            Item(f"開起來（跟雙擊 {self.program.launcher} 一樣）", self._launch, enabled=can("launch")),
            Item(lambda item: "結束程式（服務會自己再開起來）" if under_service() else "結束程式",
                 self._end, enabled=can("end")),
            Menu.SEPARATOR,
            Item("服務：停止（nssm stop）", self._service("stop"), enabled=can("stop")),
            Item("服務：啟動（nssm start）", self._service("start"), enabled=can("start")),
            Item("服務：重新啟動（nssm restart）", self._service("restart"), enabled=can("restart")),
            Item("安裝成服務（nssm install；會跳「是否允許」）", self._install, enabled=can("install"),
                 visible=can("install")),
            Menu.SEPARATOR,
        ]
        if self.program is runctl.WEB:
            items.append(Item("打開管理網頁", self._open_web, default=True))
        # 刻意沒有「關掉這個小圖示」（擁有者 2026-10-09）：圖示是小主管程式，服務停了也要在，不然沒地方開回來。
        # 它只在登出 Windows 時結束；登入時「啟動」資料夾的捷徑再把它開起來。
        items.append(Item("打開紀錄檔", self._open_log))
        return pystray.Menu(*items)

    def _launch(self, icon, item) -> None:
        bat = self.paths.root / "scripts" / "windows" / self.program.launcher
        try:
            os.startfile(bat)  # noqa: S606 - 跟雙擊一模一樣：開一個黑色視窗
        except OSError as exc:
            self._toast(f"開不起來：{exc}")
        self._kick()

    def _end(self, icon, item) -> None:
        self._expect_down = True
        if not runctl.request_end(self.paths, self.program):
            self._toast("它沒有回應「結束」（比較舊的版本開的？）——服務的話按「服務：重新啟動」或「停止」，"
                        "雙擊開的就關掉那個視窗")
        self._kick()

    def _service(self, action: str):
        def run(icon, item) -> None:
            if action in ("stop", "restart"):
                self._expect_down = True

            def work() -> None:
                ok, text = control_service(self.paths, self.program, action)
                log.info("服務 %s %s：%s %s", self.program.service, action, "成功" if ok else "失敗", text)
                if not ok:
                    self._toast(f"服務{ACTION_ZH[action]}失敗：{text}"[:250])
                self._kick()

            threading.Thread(target=work, daemon=True).start()
            self._kick()

        return run

    def _open_web(self, icon, item) -> None:
        run = runctl.running(self.paths, runctl.WEB) or {}
        webbrowser.open(run.get("detail") or "http://127.0.0.1:8787")

    def _open_log(self, icon, item) -> None:
        log_file = self.paths.root / "data" / "church_bot.log"
        if log_file.exists():
            os.startfile(log_file)  # noqa: S606 - 用記事本之類打開，跟雙擊一樣

    def _install(self, icon, item) -> None:
        """註冊成服務：開一個看得到的視窗跑 service.ps1 install（它自己會跳 UAC、問完會停在那裡讓你看結果）。"""
        script = self.paths.root / "scripts" / "windows" / "service" / "service.ps1"
        try:
            subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                              "-Action", "install", "-Target", self.program.key],
                             cwd=self.paths.root, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        except OSError as exc:
            self._toast(f"開不起來：{exc}")
        self._kick()

    def _toast(self, text: str) -> None:
        try:
            if self.icon is not None:
                self.icon.notify(text[:250], self.program.title)
        except Exception:  # noqa: BLE001 - 跳不出通知不影響其他事
            log.exception("跳通知失敗")

    # -- 狀態

    def _kick(self) -> None:
        """剛按了東西：馬上重看一次，不用等 5 秒。"""
        self._refresh_now.set()

    def refresh(self) -> None:
        before, self.status = self.status, self.snapshot()
        if self.icon is None or before == self.status:
            return
        self.icon.icon = render(self.program, self.status.color)
        self.icon.title = self.status.line[:120]  # Windows 的提示文字最多 127 字
        self.icon.update_menu()
        if before.alive and not self.status.alive:
            if self._expect_down:
                self._expect_down = False
            else:
                self._toast(f"停了！{self.status.line}")
        elif self.status.alive:
            self._expect_down = False

    def _loop(self, icon) -> None:
        icon.visible = True
        while not self._stop.is_set():
            # 平常每 5 秒；剛按了東西就馬上，之後再連看幾次（服務起停要幾秒）
            woke = self._refresh_now.wait(REFRESH_SECONDS)
            self._refresh_now.clear()
            try:
                self.refresh()
            except Exception:  # noqa: BLE001 - 查一次失敗，下一輪再查
                log.exception("更新小圖示狀態失敗")
            if woke:
                for _ in range(10):
                    if self._stop.wait(1.0):
                        return
                    self.refresh()

    def run(self) -> None:
        import pystray

        self.icon = pystray.Icon(f"church-bot-{self.program.key}", render(self.program, self.status.color),
                                 self.status.line[:120], self._menu())
        self.icon.run(setup=lambda icon: threading.Thread(target=self._loop, args=(icon,), daemon=True).start())


# --------------------------------------------------------------------------- 開起來


def _single_instance(paths: Paths, program: runctl.Program):
    """同一個人、同一份專案，一種小圖示只開一個。已經有了回 None。"""
    if sys.platform != "win32":
        return object()
    import ctypes

    name = runctl.event_name(paths, program, "Local").replace("church-bot-end-", "church-bot-tray-")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle or ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle  # 留著不關：程式結束時 Windows 自己收


def _setup_log(paths: Paths) -> None:
    target = paths.church.root / "data" / "tray.log"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size > 512 * 1024:
            target.unlink()  # 只記按了什麼，夠小；太大就重來
        logging.basicConfig(filename=target, encoding="utf-8", level=logging.INFO,
                            format="%(asctime)s %(levelname)s [%(process)d] %(message)s")
    except OSError:
        pass


def launch_all(paths: Paths) -> None:
    """兩個小圖示都開起來（各自一個 pythonw，已經開著的那個自己會退出）。"""
    script = paths.church.root / "scripts" / "windows" / "tray.pyw"
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        pythonw = Path(sys.executable)
    for program in runctl.PROGRAMS.values():
        subprocess.Popen([str(pythonw), str(script), program.key], cwd=paths.church.root,
                         creationflags=CREATE_NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    paths = Paths.discover()
    _setup_log(paths)
    if not argv:
        launch_all(paths)
        return 0
    program = runctl.PROGRAMS.get(argv[0])
    if program is None:
        log.error("不認得：%s（可以是 %s）", argv[0], "、".join(runctl.PROGRAMS))  # pythonw 沒有畫面可以印
        return 2
    guard = _single_instance(paths, program)
    if guard is None:
        return 0  # 已經有一個了
    log.info("小圖示開起來了：%s", program.title)
    Tray(paths, program).run()
    return 0
