"""免費模式：開一個 Cloudflare 臨時網址，自動登記成 LINE 的 Webhook URL。

開著的時候，群組打 /提醒、/我的ID、/我的名字 機器人都會回（Reply 免費）；關掉就停。
以前是「cloudflared 的輸出 → PowerShell 過濾 → 呼叫 Python」，PowerShell 讀管線會一次攢一大段才處理、
還會把 Python 的輸出卡住看不到，實測會登記失敗；現在直接由 Python 開 cloudflared、一行一行讀它的輸出。
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import time
from typing import Callable

import httpx

from church_bot.config import Paths, load_settings
from church_bot.errors import ChurchBotError, MessengerError

log = logging.getLogger(__name__)

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
READY_RE = re.compile(r"Registered tunnel connection")
ERROR_RE = re.compile(r"\b(ERR|error)\b")
WEBHOOK_PATH = "/line/webhook"
# 臨時網址剛建好時，外面（包括 LINE）要過十幾到幾十秒才找得到；太早登記 LINE 會回「Invalid webhook endpoint URL」
REACHABLE_TIMEOUT = 90
REGISTER_TRIES = 12
REGISTER_WAIT = 5.0


def is_reachable(url: str) -> bool:
    """從外面連得到這個網址嗎（有任何回應就算，401 也算：代表已經連到我們的管理網頁）。"""
    try:
        httpx.get(url, timeout=5)
    except httpx.HTTPError:
        return False
    return True


def register_webhook(paths: Paths, url: str, out: Callable[[str], None], *, tries: int = REGISTER_TRIES,
                     wait: float = REGISTER_WAIT, reachable: Callable[[str], bool] | None = None,
                     sleep: Callable[[float], None] = time.sleep) -> bool:
    """把網址登記成 LINE 的 Webhook URL，再請 LINE 測試連線（取代「複製網址 → 到 LINE Developers 貼上 → 按 Verify」）。

    成功（登記好、LINE 連得到、Use webhook 開著）回傳 True；任何一步有問題都印出白話說明，不會丟例外。
    """
    from church_bot.messengers.line import LineMessenger

    url = url.strip().rstrip("/")
    if not url.endswith(WEBHOOK_PATH):
        url += WEBHOOK_PATH
    base = url[: -len(WEBHOOK_PATH)]
    reachable = reachable or is_reachable
    deadline = time.monotonic() + REACHABLE_TIMEOUT
    while not reachable(base + "/"):
        if time.monotonic() >= deadline:
            out("⚠️ 臨時網址一直連不到，還是先試著登記看看")
            break
        sleep(wait)

    settings = load_settings(paths)
    try:
        messenger = LineMessenger(settings.line.channel_access_token, settings.line.timeout_seconds)
    except ChurchBotError as exc:
        out(f"❌ {exc.message}")
        return False
    ok, detail, active = False, "", False
    try:
        for attempt in range(tries):
            try:
                messenger.set_webhook(url)
                break
            except MessengerError as exc:  # 400 = LINE 還找不到這個剛建好的網址，等一下再試
                if exc.status_code != 400 or attempt == tries - 1:
                    raise
                if attempt == 0:
                    out("⏳ LINE 還找不到這個剛建好的網址，等它生效（通常不到一分鐘）...")
                sleep(wait)
        log.info("已把 LINE Webhook URL 登記成 %s", url)
        out(f"✅ 已自動登記到 LINE：{url}")
        _endpoint, active = messenger.webhook_info()
        for attempt in range(tries):
            ok, detail = messenger.test_webhook()
            if ok or attempt == tries - 1:
                break
            sleep(wait)
    except ChurchBotError as exc:
        out(f"❌ 登記到 LINE 失敗：{exc.message}")
        log.error("登記 LINE Webhook URL 失敗：%s", exc.message)
        return False
    finally:
        messenger.close()
    log.info("LINE Webhook 測試：%s（%s），Use webhook：%s", "成功" if ok else "失敗", detail, active)
    out("✅ LINE 連得到機器人了" if ok else f"⚠️ LINE 測試連線失敗（{detail}），確認這個視窗和管理網頁都開著")
    if not active:
        out("⚠️ LINE 後台的「Use webhook」是關的，群組打指令機器人不會回。")
        out("   到 LINE Developers → 你的 Channel → Messaging API → 打開「Use webhook」（只要開一次）。")
    return ok and active


def copy_to_clipboard(text: str) -> None:
    if sys.platform != "win32":
        return
    try:
        subprocess.run(["clip"], input=text.encode("utf-16-le"), timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def run_tunnel(command: list[str], register: Callable[[str], bool], out: Callable[[str], None]) -> int:
    """開 cloudflared（command），等臨時網址建好就呼叫 register(webhook 網址)；一直開著直到 cloudflared 結束或 Ctrl+C。"""
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", bufsize=1)
    except OSError as exc:
        out(f"❌ 開不了 cloudflared：{exc}")
        return 2
    webhook = ""
    registered = False
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if not webhook and (m := URL_RE.search(line)):
                webhook = m.group(0) + WEBHOOK_PATH
                log.info("臨時網址建好了：%s", webhook)
            elif webhook and not registered and READY_RE.search(line):
                registered = True
                _announce(webhook, register(webhook), out)
            elif ERROR_RE.search(line):
                out(line.rstrip())
        return proc.wait()
    except KeyboardInterrupt:
        return 0
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def _announce(webhook: str, ok: bool, out: Callable[[str], None]) -> None:
    line = "=" * 62
    out("")
    out(line)
    if ok:
        out(" ✅ 免費模式開好了！（網址已自動登記到 LINE，不用手動貼）")
        out("")
        out(" 現在可以在群組打：/提醒、/我的ID、/我的名字 你的名字")
    else:
        copy_to_clipboard(webhook)
        out(" ⚠️ 自動登記沒有完全成功（看上面的說明）。也可以手動貼（已複製）：")
        out("")
        out(f"   {webhook}")
        out("")
        out(" LINE Developers → Messaging API → Webhook URL 貼上 → 按 Verify → 打開 Use webhook。")
    out(" 用完直接關掉這個視窗就好；每次重開網址都會變，會再自動登記一次。")
    out(line)
    out("")
