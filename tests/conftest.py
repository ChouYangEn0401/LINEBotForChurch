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
