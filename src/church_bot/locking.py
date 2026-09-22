"""跨程式的鎖：同一時間只讓一個程式「真的發送」。

發送可能來自好幾個地方：Telegram 叫的 cli.bat、2-start 開著時的內建排程、網頁按鈕。
防重複（History.skip_reason）是「送之前先查有沒有送過」，兩個程式剛好同一秒一起查，會都以為還沒送。
所以發送的整段（查 → 送 → 記錄）要排隊：拿到鎖的先做，後面的等它記錄完，再查就會看到「已經送過」。
"""

from __future__ import annotations

import logging
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator

log = logging.getLogger(__name__)

LOCK_TIMEOUT_SECONDS = 300  # 讀服事表 + 送 LINE 正常幾秒就好；等到這麼久代表前一個卡住了

if sys.platform == "win32":
    import msvcrt

    def _lock_once(f: IO[bytes]) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(f: IO[bytes]) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_once(f: IO[bytes]) -> None:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(f: IO[bytes]) -> None:
        fcntl.flock(f, fcntl.LOCK_UN)


@contextmanager
def process_lock(path: Path, timeout: float = LOCK_TIMEOUT_SECONDS) -> Iterator[bool]:
    """拿到鎖回傳 True。等超過 timeout 就不等了、回傳 False 讓呼叫的人照常執行
    ——寧可極少數情況重複發一次，也不要因為前一個程式卡住而整週漏發。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as f:
        deadline = time.monotonic() + timeout
        locked = False
        while True:
            try:
                _lock_once(f)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    log.warning("等另一個發送程式等了 %d 秒還沒結束，不等了，直接執行", timeout)
                    break
                time.sleep(0.2)
        try:
            yield locked
        finally:
            if locked:
                _unlock(f)
