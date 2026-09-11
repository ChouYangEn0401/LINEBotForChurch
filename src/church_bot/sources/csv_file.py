"""本機 CSV 檔當服事表。適合：先試用、沒網路時測試、或服事表本來就在 Excel。"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from church_bot.errors import SourceError, TableError
from church_bot.models import RawSheet
from church_bot.tables import read_csv_text


class CsvFileSource:
    def __init__(self, path: Path) -> None:
        self.path = path

    def describe(self) -> str:
        return f"本機檔案：{self.path.name}"

    def fetch(self) -> list[RawSheet]:
        if not self.path.exists():
            raise SourceError(
                f"找不到服事表檔案：{self.path}",
                "到網頁「設定」確認檔案路徑；或改用 Google Sheet。",
            )
        try:
            text, _ = read_csv_text(self.path)
        except TableError as exc:
            raise SourceError(exc.message, exc.hint) from exc
        rows = list(csv.reader(io.StringIO(text)))
        return [RawSheet(rows=rows, source=self.describe())]
