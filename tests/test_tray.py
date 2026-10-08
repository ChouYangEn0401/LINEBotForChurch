"""右下角小圖示（tray.py）和它跟兩個常駐程式之間的聯絡（runctl.py）。

小圖示本身（pystray 的視窗）不在這裡測：要真的桌面。這裡測的是它「看到什麼、能按什麼」，
以及「結束程式」那個訊號真的叫得醒程式——用真的 Windows 事件，不是 monkeypatch。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from church_bot import cli, runctl, tray

on_windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows 事件／服務")


# --------------------------------------------------------------------------- 在跑的紀錄


def test_a_running_program_is_seen_and_forgotten_when_it_stops(paths, monkeypatch):
    monkeypatch.delenv("CHURCH_BOT_SERVICE", raising=False)
    assert runctl.running(paths, runctl.WEB) is None
    runctl.mark_running(paths, runctl.WEB, detail="http://127.0.0.1:8787")
    record = runctl.running(paths, runctl.WEB)
    assert record["pid"] == os.getpid() and record["mode"] == "manual"
    assert record["detail"] == "http://127.0.0.1:8787"
    runctl.mark_stopped(paths, runctl.WEB)
    assert runctl.running(paths, runctl.WEB) is None
    assert not runctl.run_file(paths, runctl.WEB).exists()


def test_the_service_says_so(paths, monkeypatch):
    monkeypatch.setenv("CHURCH_BOT_SERVICE", "1")
    runctl.mark_running(paths, runctl.WEBHOOK)
    assert runctl.running(paths, runctl.WEBHOOK)["mode"] == "service"


def test_a_dead_program_is_not_running(paths):
    """當掉的時候來不及刪紀錄：pid 已經不在，就不能說它在跑。"""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    runctl.mark_running(paths, runctl.WEB, pid=proc.pid)
    assert runctl.running(paths, runctl.WEB) is None


def test_a_reused_pid_is_not_mistaken_for_the_program(paths):
    """pid 被別的程式拿去用了：建立時間對不上，不算。"""
    runctl.mark_running(paths, runctl.WEB)
    target = runctl.run_file(paths, runctl.WEB)
    record = json.loads(target.read_text(encoding="utf-8"))
    record["created"] -= 3600
    target.write_text(json.dumps(record), encoding="utf-8")
    assert runctl.running(paths, runctl.WEB) is None


def test_stopping_only_removes_its_own_record(paths):
    """服務開著時又雙擊了一次、那一個沒開成：它結束時不能把服務的紀錄刪掉。"""
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        runctl.mark_running(paths, runctl.WEB, pid=other.pid)
        runctl.mark_stopped(paths, runctl.WEB)  # 這個測試程式自己的 pid，不是 other
        assert runctl.running(paths, runctl.WEB)["pid"] == other.pid
    finally:
        other.kill()


# --------------------------------------------------------------------------- 「請結束」


@on_windows
def test_end_from_the_tray_wakes_the_program(paths):
    woke = threading.Event()
    assert runctl.listen_for_end(paths, runctl.WEB, woke.set)
    assert runctl.request_end(paths, runctl.WEB)
    assert woke.wait(5)


@on_windows
def test_ending_a_program_that_is_not_running_says_so(paths):
    assert runctl.request_end(paths, runctl.WEBHOOK) is False


@on_windows
def test_another_checkout_is_not_ended_by_mistake(paths, tmp_path):
    """同一台電腦上另一份專案（例如 worktree）的同名程式，不會被這一份的小圖示按到。"""
    from church_bot.config import Paths

    elsewhere = Paths(tmp_path / "another-copy")
    woke = threading.Event()
    runctl.listen_for_end(paths, runctl.WEB, woke.set)
    assert runctl.request_end(elsewhere, runctl.WEB) is False
    assert not woke.wait(0.5)


@on_windows
@pytest.mark.parametrize("service, expected", [("1", "True False"), ("", "None")])
def test_a_service_ignores_somebody_logging_off(service, expected):
    """服務底下的程式會收到「有人登出」；不接住的話預設就是結束程式。只吃掉登出，Ctrl+C 照舊。"""
    code = ("from church_bot import runctl; runctl.ignore_logoff_when_service(); h = runctl._logoff_handler; "
            "print('None' if h is None else f'{bool(h(5))} {bool(h(0))}')")
    env = {**os.environ, "CHURCH_BOT_SERVICE": service, "PYTHONPATH": str(runctl.Path(cli.__file__).parents[1])}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=30)
    assert out.stdout.strip() == expected, out.stderr


# --------------------------------------------------------------------------- 小圖示看到什麼


@pytest.mark.parametrize("service, run, color, alive, words", [
    ("running", {"mode": "service"}, "service", True, "Windows 服務"),
    (None, {"mode": "manual"}, "manual", True, "2-start.bat 開的"),
    ("stopped", {"mode": "manual"}, "manual", True, "2-start.bat 開的"),  # 服務停著、自己雙擊開的
    ("start_pending", None, "busy", False, "正在啟動"),
    ("stop_pending", None, "busy", False, "正在停止"),
    ("running", None, "trouble", False, "程式沒在跑"),  # nssm 正在重開它
    ("stopped", None, "down", False, "服務已停止"),
    (None, None, "down", False, "沒在跑"),
])
def test_what_the_icon_shows(service, run, color, alive, words):
    status = tray.describe(runctl.WEB, service, run)
    assert (status.color, status.alive) == (color, alive)
    assert words in status.line and status.line.startswith(runctl.WEB.title)


def test_menu_follows_the_state():
    running = tray.menu_state(tray.describe(runctl.WEB, "running", {"mode": "service"}))
    assert running == {"end": True, "stop": True, "start": False, "restart": True}
    stopped = tray.menu_state(tray.describe(runctl.WEB, "stopped", None))
    assert stopped == {"end": False, "stop": False, "start": True, "restart": True}
    manual_only = tray.menu_state(tray.describe(runctl.WEB, None, {"mode": "manual"}))
    assert manual_only == {"end": True, "stop": False, "start": False, "restart": False}  # 沒註冊成服務


@pytest.mark.parametrize("color", list(tray.COLORS))
def test_every_state_has_an_icon(color):
    for program in runctl.PROGRAMS.values():
        image = tray.render(program, color)
        assert image.size == (64, 64)
        assert image.getpixel((32, 6))[:3] == tray.COLORS[color]  # 顏色就是狀態


def test_service_control_explains_when_nothing_is_registered(paths):
    ok, text = tray.control_service(paths, runctl.WEB, "stop")
    assert not ok and "install.bat" in text


@pytest.mark.parametrize("text, denied", [
    ("OpenService(): Access is denied.", True),
    ("church-bot: STOP: 拒絕存取。", True),
    ("church-bot: START: 服務已經在執行中。", False),
])
def test_access_denied_is_recognised(text, denied):
    assert tray._denied(text) is denied


# --------------------------------------------------------------------------- LINE 指令（臨時網址）


def _tunnel_args(tmp_path):
    return argparse.Namespace(port=8787, cloudflared=str(tmp_path / "cloudflared.exe"))


def test_tunnel_refuses_when_the_service_already_runs_it(paths, monkeypatch, capsys, tmp_path):
    """兩條臨時網址會互相把對方從 LINE 的 Webhook 擠掉，所以服務在跑時不再開第二條。"""
    monkeypatch.setattr(cli, "_service_running", lambda name=cli.SERVICE_NAME: name == runctl.WEBHOOK.service)
    assert cli.cmd_tunnel(paths, _tunnel_args(tmp_path)) == cli.PORT_IN_USE
    assert "已經註冊成 Windows 服務" in capsys.readouterr().out
    assert runctl.running(paths, runctl.WEBHOOK) is None


@on_windows
def test_tunnel_can_be_ended_from_the_tray(paths, monkeypatch, tmp_path):
    import church_bot.tunnel as tunnel_mod

    monkeypatch.setattr(cli, "_service_running", lambda name=cli.SERVICE_NAME: False)

    def fake_run_tunnel(command, register, out, started=None):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        started(proc)
        return proc.wait()

    monkeypatch.setattr(tunnel_mod, "run_tunnel", fake_run_tunnel)
    result: dict = {}
    worker = threading.Thread(target=lambda: result.update(code=cli.cmd_tunnel(paths, _tunnel_args(tmp_path))))
    worker.start()
    deadline = time.monotonic() + 10
    while runctl.running(paths, runctl.WEBHOOK) is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert runctl.running(paths, runctl.WEBHOOK)["pid"] == os.getpid()
    time.sleep(0.3)  # 讓 cloudflared（假的）先開起來
    assert runctl.request_end(paths, runctl.WEBHOOK)
    worker.join(15)
    assert result["code"] == runctl.ENDED_FROM_TRAY  # 3-open-webhook.bat 看到這個就直接關、不等按鍵
    assert runctl.running(paths, runctl.WEBHOOK) is None
