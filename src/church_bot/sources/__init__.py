"""服事表資料來源。依設定 ``source.kind`` 建立對應的實作。"""

from __future__ import annotations

from church_bot.config import Paths, SourceSettings
from church_bot.errors import ConfigError
from church_bot.sources.base import RosterSource
from church_bot.sources.csv_file import CsvFileSource
from church_bot.sources.google_public import GooglePublicSheetSource
from church_bot.sources.google_service_account import GoogleServiceAccountSource

SOURCE_KINDS_ZH = {
    "google_public": "Google Sheet（公開連結，最簡單）",
    "google_service_account": "Google Sheet（服務帳號，不公開）",
    "csv": "本機 CSV 檔（試用 / 測試）",
}


def build_source(cfg: SourceSettings, paths: Paths) -> RosterSource:
    if cfg.kind == "google_public":
        return GooglePublicSheetSource(cfg.spreadsheet_url, cfg.worksheet, cfg.timeout_seconds)
    if cfg.kind == "google_service_account":
        return GoogleServiceAccountSource(cfg.spreadsheet_url, cfg.worksheet, paths.resolve(cfg.credentials_file))
    if cfg.kind == "csv":
        return CsvFileSource(paths.resolve(cfg.csv_path))
    raise ConfigError(f"不認得的資料來源：{cfg.kind}", f"可以用：{'、'.join(SOURCE_KINDS_ZH)}")


__all__ = ["RosterSource", "SOURCE_KINDS_ZH", "build_source"]
