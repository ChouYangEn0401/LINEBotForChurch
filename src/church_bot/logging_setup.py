"""Log 設定：畫面 + data/church_bot.log（自動輪替，最多 5 個 1MB 檔）。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from church_bot.config import Paths

_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"


def setup_logging(paths: Paths, verbose: bool = False) -> None:
    root = logging.getLogger()
    if getattr(root, "_church_bot_configured", False):
        return
    paths.data_dir.mkdir(parents=True, exist_ok=True)

    # Windows 中文環境的命令列預設是 cp950，印 emoji 會整個當掉；這裡改成「印不出來就用 ? 代替」
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    file_handler = RotatingFileHandler(paths.log_file, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(_FORMAT))
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))

    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console)
    for noisy in ("httpx", "httpcore", "apscheduler", "urllib3", "google", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    root._church_bot_configured = True  # type: ignore[attr-defined]
