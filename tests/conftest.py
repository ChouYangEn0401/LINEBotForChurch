from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from church_bot.config import Paths
from church_bot.errors import MessengerError
from church_bot.messengers.base import Quota, SendResult
from church_bot.models import OutgoingMessage

TODAY = dt.date(2026, 9, 11)  # 星期五；最近的主日是 9/13


def gid(ch: str = "a") -> str:
    return "C" + ch * 32


def uid(ch: str = "b") -> str:
    return "U" + ch * 32


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
def paths(tmp_path: Path) -> Paths:
    (tmp_path / "config").mkdir()
    (tmp_path / "data").mkdir()
    return Paths(tmp_path)


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client(paths: Paths):
    """管理網頁（測試模式發送、關掉排程），資料用 cmd_init 建出來的範例。"""
    from fastapi.testclient import TestClient

    from church_bot.cli import cmd_init
    from church_bot.config import load_settings, save_settings
    from church_bot.web.app import create_app

    for name in ("settings.example.yaml", "targets.example.csv", "members.example.csv"):
        (paths.config_dir / name).write_bytes((REPO_ROOT / "config" / name).read_bytes())
    (paths.root / ".env.example").write_bytes((REPO_ROOT / ".env.example").read_bytes())
    cmd_init(paths, None)
    settings = load_settings(paths)
    settings.messenger.kind = "console"
    settings.schedule.enabled = False
    save_settings(paths, settings)
    with TestClient(create_app(paths)) as c:
        yield c
