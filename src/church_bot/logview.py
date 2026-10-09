"""看即時紀錄：開一個視窗，一直印 data/church_bot.log 最新的內容——跟以前雙擊 2-start 時黑色視窗裡看到的差不多，
程式是 Windows 服務、沒有黑色視窗的時候也看得到。關掉這個視窗不會影響程式（它只是在讀檔）。

    python -m church_bot.logview web       （小圖示「L」的「看即時紀錄」開的就是這個）
    python -m church_bot.logview webhook   （「Lw」：LINE 指令也寫在同一個紀錄檔）

**每次讀完就關檔**（不一直開著）：紀錄檔滿 1MB 會換檔（church_bot.log → .1），一直開著的話 Windows 會擋住換檔。
檔案變短了＝換過檔，從頭讀。跟 MyFirstTelegramClawBot 的 clients/logview.py 同一個做法（docs/tray-standard.html）。
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from pathlib import Path

from church_bot import runctl
from church_bot.config import Paths

TAIL_LINES = 40
POLL_SECONDS = 0.5


def last_lines(path: Path, count: int = TAIL_LINES) -> tuple[list[str], int]:
    """(最後 count 行, 檔案大小)。只讀檔尾一段。"""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            fh.seek(max(0, size - 64 * 1024))
            data = fh.read()
    except OSError:
        return [], 0
    return data.decode("utf-8", errors="replace").splitlines()[-count:], size


def read_from(path: Path, offset: int) -> tuple[str, int]:
    """從 offset 讀到檔尾：(新的文字, 新的 offset)。檔案比 offset 短＝換過檔，從頭讀。"""
    try:
        size = path.stat().st_size
        if size < offset:
            offset = 0
        if size == offset:
            return "", offset
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read(size - offset)
    except OSError:
        return "", offset
    return data.decode("utf-8", errors="replace"), offset + len(data)


def follow(paths: Paths, program: runctl.Program) -> None:
    title = f"{program.title} 即時紀錄"
    if sys.platform == "win32":
        ctypes.windll.kernel32.SetConsoleTitleW(title)
    path = paths.church.log_file
    lines, offset = last_lines(path)
    print(f"{title}　{path}\n（關掉這個視窗不會影響程式）\n" + "-" * 60, flush=True)
    print("\n".join(lines) if lines else "（還沒有內容）", flush=True)
    while True:
        time.sleep(POLL_SECONDS)
        text, offset = read_from(path, offset)
        if text:
            print(text, end="", flush=True)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = list(sys.argv[1:] if argv is None else argv)
    program = runctl.PROGRAMS.get(args[0] if args else "web", runctl.WEB)
    try:
        follow(Paths.discover(), program)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
