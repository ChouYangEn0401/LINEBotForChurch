"""用 Google 服務帳號讀「不公開」的 Google Sheet。

比公開連結多兩個步驟：
1. 在 Google Cloud 建一個服務帳號、下載金鑰 JSON，放到 config/service-account.json
2. 把服事表「共用」給那個服務帳號的 email（…@….iam.gserviceaccount.com，檢視者即可）

分頁名稱可以一次填多個（用逗號隔開），或填 * 代表全部分頁一起讀。
"""

from __future__ import annotations

import json
from pathlib import Path

from church_bot.errors import ConfigError, SourceError
from church_bot.models import RawSheet
from church_bot.sources.base import split_worksheets
from church_bot.sources.google_public import parse_sheet_url


def service_account_email(credentials_file: Path) -> str:
    try:
        return json.loads(credentials_file.read_text(encoding="utf-8")).get("client_email", "")
    except (OSError, ValueError):
        return ""


class GoogleServiceAccountSource:
    def __init__(self, url: str, worksheet: str, credentials_file: Path) -> None:
        self.url = url
        self.sheet_id, self.gid, published = parse_sheet_url(url)
        if published:
            raise ConfigError("服務帳號模式不能用「發佈到網路」的網址", "請貼一般的編輯網址（…/spreadsheets/d/…/edit）。")
        self.worksheets = split_worksheets(worksheet)
        self.credentials_file = credentials_file

    def describe(self) -> str:
        which = "、".join(self.worksheets) if self.worksheets else (f"gid={self.gid}" if self.gid else "第一個分頁")
        return f"Google Sheet（服務帳號）：{which}"

    def fetch(self) -> list[RawSheet]:
        if not self.credentials_file.exists():
            raise SourceError(
                f"找不到 Google 服務帳號金鑰：{self.credentials_file.name}",
                "把下載的 JSON 金鑰檔改名成 service-account.json 放進 config 資料夾；或改用「公開連結」模式。",
            )
        import gspread  # 延後載入：只有用這個模式才需要

        email = service_account_email(self.credentials_file)
        share_hint = f"把服事表「共用」給 {email}（檢視者）。" if email else "把服事表共用給服務帳號的 email。"
        try:
            client = gspread.service_account(filename=str(self.credentials_file))
            book = client.open_by_key(self.sheet_id)
            worksheets = self._pick(book)
            return [
                RawSheet(rows=ws.get_all_values(), source=f"Google Sheet：{book.title} / {ws.title}")
                for ws in worksheets
            ]
        except gspread.exceptions.SpreadsheetNotFound as exc:
            raise SourceError("服務帳號打不開這份 Google Sheet（沒權限或網址錯）", share_hint) from exc
        except gspread.exceptions.WorksheetNotFound as exc:
            raise SourceError(f"找不到分頁：{exc}", "分頁名稱要跟 Google Sheet 下方的分頁標籤一模一樣。") from exc
        except gspread.exceptions.APIError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", "?")
            hint = share_hint if status in (403, 404) else "稍後再試；持續發生請到 Google Cloud 確認已啟用 Google Sheets API。"
            raise SourceError(f"Google Sheets API 錯誤（HTTP {status}）", hint) from exc
        except (ValueError, KeyError) as exc:
            raise SourceError("服務帳號金鑰檔格式不對", "請重新從 Google Cloud 下載 JSON 金鑰。") from exc
        except OSError as exc:
            raise SourceError(f"連不上 Google：{exc}", "檢查這台電腦的網路。") from exc

    def _pick(self, book):  # noqa: ANN001 - gspread 型別
        if self.worksheets == ["*"]:
            return book.worksheets()
        if self.worksheets:
            return [book.worksheet(name) for name in self.worksheets]
        if self.gid:
            return [book.get_worksheet_by_id(int(self.gid))]
        return [book.sheet1]
