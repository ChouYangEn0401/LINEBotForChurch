"""右下角的小圖示：後台、LINE 指令（臨時網址）各一個。

擁有者 2026-10-09：「服務在背景運作的時候，我要能在小工具裡面看到它在運作，然後可以透過小工具去暫停服務」，
而且「不管用何種方式運作，都要讓 2 個程式顯示小圖示在下面」。選單：

    結束程式（服務模式下 nssm 會再把它開起來）／服務：停止／服務：啟動／服務：重新啟動

顏色：綠＝以 Windows 服務在跑；藍＝雙擊開的在跑；黃＝正在啟動／停止中；紅＝服務開著但程式不在
（nssm 正在重開它，或一直起不來）；灰＝沒在跑。

**怎麼知道狀態**：每 5 秒在這個程式裡面問一次 Windows（服務控制器＋那個 pid 還在不在），
不開新程式、不連網，一次不到 1 毫秒。擁有者否決了「點開選單才查」：那樣它掛了也看不到。
從「在跑」變成「不在」而且不是你按的，會跳一個通知。

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
import webbrowser
from dataclasses import dataclass
from pathlib import Path

from church_bot import runctl
from church_bot.config import Paths

log = logging.getLogger(__name__)

REFRESH_SECONDS = 5.0
COLORS = {
    "service": (46, 160, 67),    # 綠
    "manual": (31, 111, 235),    # 藍
    "busy": (210, 153, 34),      # 黃
    "trouble": (207, 34, 46),    # 紅
    "down": (140, 140, 140),     # 灰
}
GLYPHS = {"web": ("後", "B"), "webhook": ("令", "L")}  # 中文字型找不到就用英文字母
PENDING = {"start_pending": "正在啟動", "stop_pending": "正在停止",
           "continue_pending": "正在繼續", "pause_pending": "正在暫停"}
ACTION_ZH = {"stop": "停止", "start": "啟動", "restart": "重新啟動"}
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class Status:
    color: str          # COLORS 的鍵
    line: str           # 滑鼠停在上面、選單第一行看到的那一句
    alive: bool         # 程式本身在跑
    service: str | None  # 服務狀態；None＝沒註冊成服務


def describe(program: runctl.Program, service: str | None, run: dict | None) -> Status:
    """把「服務狀態」＋「在跑的紀錄」翻成一個顏色和一句話。純函式，測試直接餵。"""
    if run:
        if run.get("mode") == "service":
            return Status("service", f"{program.title}：執行中（Windows 服務）", True, service)
        return Status("manual", f"{program.title}：執行中（{program.launcher} 開的）", True, service)
    if service in PENDING:
        return Status("busy", f"{program.title}：服務{PENDING[service]}…", False, service)
    if service == "running":
        return Status("trouble", f"{program.title}：服務開著，但程式沒在跑（正在重開，或一直起不來）",
                      False, service)
    if service == "paused":
        return Status("busy", f"{program.title}：服務已暫停", False, service)
    if service == "stopped":
        return Status("down", f"{program.title}：沒在跑（服務已停止）", False, service)
    return Status("down", f"{program.title}：沒在跑", False, service)


def menu_state(status: Status) -> dict[str, bool]:
    """每個選項現在能不能按。"""
    installed = status.service is not None
    stopped = status.service == "stopped"
    return {
        "end": status.alive,
        "stop": installed and not stopped,
        "start": installed and stopped,
        "restart": installed,
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


def _font(size: int, chinese: bool):
    from PIL import ImageFont

    names = ("msjhbd.ttc", "msjh.ttc", "msyhbd.ttc") if chinese else ("segoeuib.ttf", "arialbd.ttf")
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return None


def render(program: runctl.Program, color: str, size: int = 64):
    """圓角方塊＋一個字：「後」＝後台、「令」＝LINE 指令。顏色就是狀態。"""
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, size - 3, size - 3), radius=size // 5, fill=COLORS[color] + (255,))
    chinese, latin = GLYPHS[program.key]
    font = _font(int(size * 0.62), chinese=True)
    glyph = chinese if font else latin
    font = font or _font(int(size * 0.62), chinese=False)
    if font is not None:
        draw.text((size / 2, size / 2), glyph, fill=(255, 255, 255, 255), font=font, anchor="mm")
    return image


# --------------------------------------------------------------------------- 小圖示本體


class Tray:
    def __init__(self, paths: Paths, program: runctl.Program) -> None:
        self.paths = paths.church
        self.program = program
        self.status = self.snapshot()
        self.icon = None
        self._stop = threading.Event()
        self._refresh_now = threading.Event()
        self._expect_down = False  # 是你按的（結束、停止、重開）就不要跳「停了」的通知

    def snapshot(self) -> Status:
        return describe(self.program, runctl.service_state(self.program), runctl.running(self.paths, self.program))

    # -- 選單

    def _menu(self):
        import pystray
        from pystray import Menu, MenuItem as Item

        can = lambda key: (lambda item: menu_state(self.status)[key])  # noqa: E731
        under_service = lambda: self.status.alive and self.status.service is not None  # noqa: E731
        items = [
            Item(lambda item: self.status.line, None, enabled=False),
            Menu.SEPARATOR,
            Item(lambda item: "結束程式（服務會自己再開起來）" if under_service() else "結束程式",
                 self._end, enabled=can("end")),
            Item("服務：停止（nssm stop）", self._service("stop"), enabled=can("stop")),
            Item("服務：啟動（nssm start）", self._service("start"), enabled=can("start")),
            Item("服務：重新啟動（nssm restart）", self._service("restart"), enabled=can("restart")),
            Menu.SEPARATOR,
        ]
        if self.program is runctl.WEB:
            items.append(Item("打開管理網頁", self._open_web, default=True))
        items += [Item("打開紀錄檔", self._open_log),
                  Item("關掉這個小圖示（程式照常跑）", self._quit)]
        return pystray.Menu(*items)

    def _end(self, icon, item) -> None:
        self._expect_down = True
        if not runctl.request_end(self.paths, self.program):
            self._toast("它現在沒在跑，或是比較舊的版本開的（關掉那個視窗就好）")
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

    def _quit(self, icon, item) -> None:
        self._stop.set()
        self._refresh_now.set()
        icon.stop()

    def _toast(self, text: str) -> None:
        try:
            if self.icon is not None:
                self.icon.notify(text, self.program.title)
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
