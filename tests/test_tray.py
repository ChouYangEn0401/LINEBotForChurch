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

SERVICE_RUN = {"mode": "service"}
MANUAL_RUN = {"mode": "manual"}


@pytest.mark.parametrize("service, run, app, stuck, color, alive, words", [
    ("running", SERVICE_RUN, True, False, "service", True, "Windows 服務"),
    # 2026-10-09 擁有者看到的那一種：服務開著、程式是舊版（沒寫紀錄）——nssm 底下有程式就算在跑
    ("running", None, True, False, "service", True, "Windows 服務"),
    (None, MANUAL_RUN, False, False, "manual", True, "2-start.bat 開的"),
    ("stopped", MANUAL_RUN, False, False, "manual", True, "2-start.bat 開的"),  # 服務停著、自己雙擊開的
    ("start_pending", None, False, False, "busy", False, "正在啟動"),
    ("stop_pending", None, False, False, "busy", False, "正在停止"),
    ("stop_pending", None, False, True, "busy", False, "超過 1 分鐘"),  # 卡在正在停止（真的發生過）
    ("running", None, False, False, "busy", False, "程式沒回應"),  # nssm 正在重開它
    ("paused", None, False, False, "busy", False, "已暫停"),
    ("stopped", None, False, False, "down", False, "服務已停止"),
    (None, None, False, False, "down", False, "還沒註冊成服務"),
])
def test_what_the_icon_shows(service, run, app, stuck, color, alive, words):
    status = tray.describe(runctl.WEB, service, run, service_app=app, stuck=stuck)
    assert (status.color, status.alive) == (color, alive)
    assert words in status.line and status.line.startswith("LINE · 後台")


ALL_STATES = [(service, run, app, stuck)
              for service in (None, "running", "stopped", "paused", "start_pending", "stop_pending")
              for run in (None, SERVICE_RUN, MANUAL_RUN)
              for app in (False, True) if not (app and service != "running")
              for stuck in (False, True) if not (stuck and service not in tray.PENDING)]


@pytest.mark.parametrize("service, run, app, stuck", ALL_STATES)
def test_no_state_leaves_you_without_something_to_press(service, run, app, stuck):
    """看得到狀態、卻什麼都不能按，這個圖示就沒用了。唯一例外：正在起停的頭 1 分鐘先灰掉免得連按。"""
    status = tray.describe(runctl.WEB, service, run, service_app=app, stuck=stuck)
    can = tray.menu_state(status)
    if service in tray.PENDING and not stuck and not status.alive:
        return
    assert any(can.values()), (status.line, can)


@pytest.mark.parametrize("service, run, app, stuck", ALL_STATES)
def test_the_sentence_and_the_menu_agree(service, run, app, stuck):
    """那一句話叫你按什麼，那個選項就一定按得到。"""
    status = tray.describe(runctl.WEB, service, run, service_app=app, stuck=stuck)
    can = tray.menu_state(status)
    for words, key in (("「服務：啟動」", "start"), ("「服務：重新啟動」", "restart"), ("「開起來」", "launch")):
        if words in status.line:
            assert can[key], (status.line, can)


def test_menu_follows_the_state():
    def can(*args, **kwargs):
        return tray.menu_state(tray.describe(runctl.WEB, *args, **kwargs))

    assert can("running", SERVICE_RUN) == {"end": True, "stop": True, "start": False, "restart": True,
                                           "launch": False, "install": False}
    # 服務停著：可以用服務開，也可以手動（雙擊的方式）開——擁有者：「如果服務沒開我可以自己手動啟動」
    assert can("stopped", None) == {"end": False, "stop": False, "start": True, "restart": True, "launch": True,
                                    "install": False}
    # 服務開著、程式沒回應：啟動按不了（已經開著），但重新啟動、停止都可以
    assert can("running", None) == {"end": False, "stop": True, "start": False, "restart": True, "launch": False,
                                    "install": False}
    # 沒註冊成服務、雙擊開的：只能結束
    assert can(None, MANUAL_RUN) == {"end": True, "stop": False, "start": False, "restart": False, "launch": False,
                                     "install": True}
    # 什麼都沒有：可以用雙擊的方式開起來
    assert can(None, None)["launch"] is True and can(None, None)["install"] is True
    # 服務停著、但雙擊開的那一份開著：不給啟動服務（會搶 port）
    assert can("stopped", MANUAL_RUN)["start"] is False
    # 正在停止：先灰掉；卡住了就把停止、重新啟動還回來
    assert not any(can("stop_pending", None).values())
    stuck = can("stop_pending", None, stuck=True)
    assert stuck["stop"] and stuck["restart"]


@pytest.mark.parametrize("color", list(tray.COLORS))
def test_every_state_has_an_icon(color):
    for program in runctl.PROGRAMS.values():
        image = tray.render(program, color)
        assert image.size == (64, 64)
        assert image.getpixel((32, 6))[:3] == tray.COLORS[color]  # 顏色就是狀態


def test_names_follow_the_family_convention():
    """擁有者 2026-10-09：圖示＝專案字母＋元件記號，提示文字以「專案 · 元件」開頭。"""
    assert tray.GLYPHS == {"web": "L", "webhook": "Lw"}
    assert runctl.WEB.title == "LINE · 後台" and runctl.WEBHOOK.title == "LINE · webhook"


@on_windows
def test_service_app_check_does_not_need_the_programs_own_record():
    """沒註冊的服務：不在（也不會丟例外）。"""
    ghost = runctl.Program("ghost", "church-bot-no-such-service", "LINE · ghost", "x.bat")
    assert runctl.service_app_alive(ghost) is False


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


def test_the_icon_cannot_be_closed_from_its_own_menu():
    """擁有者 2026-10-09：小圖示是小主管程式，不可以不見（不然要怎麼開回來）；取消註冊也不放選單（避免誤觸）。"""
    import inspect
    source = inspect.getsource(tray.Tray._menu)
    assert 'Item("關掉' not in source and not hasattr(tray.Tray, "_quit")
    assert "uninstall" not in source
    assert "安裝成服務（nssm install" in source
