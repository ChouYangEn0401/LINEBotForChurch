"""「重新啟動」按鈕：網頁那個子程式用 RESTART_CODE 結束，外面那一層再開一次（會載入新的程式）。"""

import time
from types import SimpleNamespace

from church_bot.cli import RESTART_CODE, build_parser, cmd_web
from church_bot.config import update_env_file
from tests.conftest import CHURCH


class FakeChild:
    codes: list[int] = []
    commands: list[list[str]] = []

    def __init__(self, command):
        FakeChild.commands.append(command)

    def wait(self, timeout=None):
        return FakeChild.codes.pop(0)


def test_supervisor_starts_the_web_again_when_asked_to_restart(paths, monkeypatch):
    FakeChild.codes, FakeChild.commands = [RESTART_CODE, 0], []
    monkeypatch.setattr("church_bot.cli.subprocess.Popen", FakeChild)
    assert cmd_web(paths, build_parser().parse_args(["web", "--port", "9100"])) == 0
    first, second = FakeChild.commands
    assert first[-4:] == ["web", "--child", "--port", "9100"]  # 第一次會開瀏覽器
    assert "--no-browser" in second and "--restarted" in second  # 重開的那一次不要再開一個分頁


def test_supervisor_gives_up_if_it_keeps_restarting(paths, monkeypatch, capsys):
    FakeChild.codes, FakeChild.commands = [RESTART_CODE] * 10, []
    monkeypatch.setattr("church_bot.cli.subprocess.Popen", FakeChild)
    assert cmd_web(paths, build_parser().parse_args(["web"])) == 1
    assert "重新啟動太多次" in capsys.readouterr().out


def test_restart_button(client, monkeypatch):
    r = client.post(CHURCH + "/restart", data={"next": "/m/m1/"}, follow_redirects=False)
    assert "level=error" in r.headers["location"]  # 不是用 2-start 開的（測試裡沒有外面那一層）

    server = SimpleNamespace(should_exit=False)
    client.app.state.server = server
    monkeypatch.setattr("church_bot.ministries.Church.busy", lambda self: True)
    r = client.post(CHURCH + "/restart", data={"next": "/m/m1/"}, follow_redirects=False)
    assert "level=warn" in r.headers["location"] and not server.should_exit  # 正在發送：等它送完

    monkeypatch.setattr("church_bot.ministries.Church.busy", lambda self: False)
    boot = client.get(CHURCH + "/healthz").json()["boot"]
    page = client.post(CHURCH + "/restart", data={"next": "/m/m1/"})
    assert page.status_code == 200 and "重新啟動中" in page.text and boot in page.text
    assert client.app.state.restart_requested
    for _ in range(50):
        if server.should_exit:
            break
        time.sleep(0.05)
    assert server.should_exit


def test_only_the_server_manager_can_restart(client, paths):
    update_env_file(paths.env_file, {"SERVER_MANAGER_PASSWORD": "manager-password-123"})
    client.cookies.clear()
    client.app.state.server = SimpleNamespace(should_exit=False)
    assert client.post(CHURCH + "/restart").status_code == 403
    assert not getattr(client.app.state, "restart_requested", False)
