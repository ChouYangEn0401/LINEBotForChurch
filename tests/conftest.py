from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from church_bot.config import Paths
from church_bot.errors import MessengerError
from church_bot.messengers.base import Quota, SendResult
from church_bot.models import OutgoingMessage
from tests.line_fakes import gid, uid  # noqa: F401 - 很多測試檔從 conftest 匯入

TODAY = dt.date(2026, 9, 11)  # 星期五；最近的主日是 9/13


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class FakeMessenger:
    name = "fake"

    def __init__(self) -> None:
        self.sent: list[tuple[str, OutgoingMessage]] = []
        self.fail: dict[str, MessengerError] = {}
        self.quota_value: Quota | None = None
        self.sizes: dict[str, int] = {}

    def send(self, to: str, message: OutgoingMessage) -> SendResult:
        if to in self.fail:
            raise self.fail[to]
        self.sent.append((to, message))
        return SendResult()

    def check(self) -> str:
        return "fake bot"

    def audience_size(self, to: str) -> int | None:
        return self.sizes.get(to, 10)

    def quota(self) -> Quota | None:
        return self.quota_value

    def close(self) -> None:
        pass

    def texts_to(self, to: str) -> list[str]:
        return [m.text for t, m in self.sent if t == to]


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("LINE_CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_SECRET", "UI_PASSWORD", "CHURCH_BOT_HOME"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def root(tmp_path: Path) -> Paths:
    """整個教會那一層（還沒有任何牧區）。"""
    (tmp_path / "config").mkdir()
    (tmp_path / "data").mkdir()
    return Paths(tmp_path)


@pytest.fixture
def paths(root: Paths) -> Paths:
    """一個教會只有一個牧區（m1「測試牧區」）的資料夾：大部分測試都在這裡面跑，跟實際使用的樣子一樣。"""
    from church_bot.church import ChurchConfig, Ministry, save_church

    save_church(root, ChurchConfig([Ministry("m1", "測試牧區")]))
    m1 = root.for_ministry("m1")
    m1.config_dir.mkdir(parents=True)
    m1.data_dir.mkdir(parents=True)
    return m1


@pytest.fixture
def handler(paths: Paths, monkeypatch: pytest.MonkeyPatch):
    """LINE Webhook 處理器，LINE 換成 tests/line_fakes.py 的 FakeLine。

    gid() 這個群組已經在 m1 的「LINE 群組」清單裡（先不啟用）：大部分指令測試都假設「在自己牧區的群組裡打」。
    """
    from church_bot.models import Target
    from church_bot.tables import TargetTable
    from tests.line_fakes import gid

    TargetTable(paths.targets_file).save([Target("同工群", gid(), enabled=False)])
    from church_bot.ministries import Church
    from church_bot.webhook import WebhookHandler
    from tests.line_fakes import SECRET, FakeLine

    write(paths.env_file, f"LINE_CHANNEL_SECRET={SECRET}\nLINE_CHANNEL_ACCESS_TOKEN=tok\n")
    FakeLine.reset()
    monkeypatch.setattr("church_bot.webhook.LineMessenger", FakeLine)
    return WebhookHandler(Church(paths.church))


# --------------------------------------------------------------------------- web

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client(paths: Paths):
    """管理網頁（測試模式發送、關掉排程），資料用 cmd_init 建出來的範例。"""
    from fastapi.testclient import TestClient

    from church_bot.cli import cmd_init
    from church_bot.config import load_settings, save_settings
    from church_bot.web.app import create_app

    for name in ("settings.example.yaml", "targets.example.csv", "members.example.csv", "teams.example.csv"):
        (paths.church.config_dir / name).write_bytes((REPO_ROOT / "config" / name).read_bytes())
    (paths.root / ".env.example").write_bytes((REPO_ROOT / ".env.example").read_bytes())
    cmd_init(paths, None)
    settings = load_settings(paths)
    settings.messenger.kind = "console"
    settings.schedule.enabled = False
    save_settings(paths, settings)
    with TestClient(create_app(paths)) as c:
        yield c
