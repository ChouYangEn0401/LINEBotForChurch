# 架構說明（給維護的工程師）

## 一次執行的流程

```
            ┌─ sources/ ───────────────────────┐
 Google     │ RosterSource.fetch() → RawSheet  │   只負責「拿到格子」（全部字串）
 Sheet/CSV  └──────────────────────────────────┘
                 │
                 ▼ core/parser.py      自動判斷 wide / long / matrix → Roster（日期 × 服事 × 人）
                 ▼ core/planner.py     挑出這次的聚會、依群組表產生訊息、找出所有問題（Issue）
                 │    ├ core/directory.py  名字 → 人員表（別名、全形半形、括號註記、猜測）
                 │    └ core/renderer.py   Jinja2 模板 → 純文字 + LINE textV2（@ 人）
                 ▼ core/dispatcher.py  防重複（SQLite）→ 檢查額度 → Messenger.send() → 每則結果
            ┌─ messengers/ ────────────────────┐
            │ LineMessenger / ConsoleMessenger │   只負責「把字送出去」
            └──────────────────────────────────┘
                 ▼ service.py          記錄 RunReport、通知管理員
```

`service.BotService` 是唯一的組裝點；`web/`、`scheduler.py`、`cli.py` 都只呼叫它。
每次執行都重新讀設定與 CSV，所以改完設定、改完 Excel 不用重開程式。

## 目錄

| 檔案 | 職責 |
|---|---|
| `config.py` | `settings.yaml`（pydantic 驗證）+ `.env`（機密）；路徑 |
| `models.py` | 純資料：Roster、ServiceDay、Target、Member、Issue、RunReport… |
| `errors.py` | `ChurchBotError(message, hint)` 與子類別 |
| `tables.py` | 群組表、人員表的 CSV 讀寫（編碼自動判斷、逐列驗證） |
| `sources/` | 資料來源：`csv_file`、`google_public`（免金鑰）、`google_service_account` |
| `core/dates.py` | 日期解析（民國年、缺年份推測、Excel 序號…）與格式化 |
| `core/parser.py` | 表格 → Roster，三種排法自動判斷 |
| `core/directory.py` | 名字對照 |
| `core/renderer.py` | 訊息排版（Jinja2、textV2 mention） |
| `core/planner.py` | 決定發給誰、發什麼；產生所有 Issue |
| `core/dispatcher.py` | 防重複、額度檢查、發送、失敗處理 |
| `core/history.py` | SQLite：執行紀錄、發送紀錄、Webhook 看到的群組 |
| `messengers/` | 發送端：`line`（Messaging API）、`console`（測試模式） |
| `service.py` | 組裝一次完整執行、管理員通知、健康檢查 |
| `scheduler.py` | APScheduler 每週排程 + 開機補發 |
| `webhook.py` | LINE Webhook：「/」開頭的聊天指令（`parse_command`）、加入群組、被踢出群組、被動收集 LINE 帳號 |
| `web/` | FastAPI：管理網頁（Jinja2）+ `/api/*` JSON API |
| `cli.py` | `python -m church_bot init / web / check / preview / send` |

## 錯誤處理原則：不要沉默

1. 可預期的錯誤一律丟 `ChurchBotError` 子類別，帶 `message`（發生什麼）和 `hint`（下一步怎麼做）。
2. 一次執行的所有狀況都收集成 `Issue`（ERROR／WARNING／INFO），放進 `RunReport`。
3. `RunReport` 同時出現在：網頁首頁、執行紀錄（SQLite）、log 檔、CLI 結束代碼、管理員 LINE 通知（ERROR、WARNING）。
4. `BotService.run()` 不會往外丟例外；沒預料到的例外也會變成 ERROR issue，完整 traceback 寫進 log。
5. 一個群組失敗不影響其他群組；token 錯誤、額度用完這種「後面一定也會失敗」的錯誤，會停止繼續呼叫 LINE。
6. 已知限制：LINE 本身壞掉時（token 失效），管理員通知也送不出去，只剩網頁與 log。
   要加第二通道（例如 Email），實作一個 Messenger，在 `service._alert_admin` 裡當備援即可。

## 換掉 Google Sheet（新增資料來源）

1. 在 `sources/` 新增類別：

   ```python
   class MySource:
       def describe(self) -> str: ...
       def fetch(self) -> list[RawSheet]:  # 失敗請丟 SourceError(message, hint)，不要回傳空資料
           ...
   ```

2. `config.SourceSettings.kind` 的 `Literal` 加上新名字；需要的參數也加在 `SourceSettings`。
3. `sources/__init__.py` 的 `build_source()` 和 `SOURCE_KINDS_ZH` 各加一行。

解析、對照、發送都不用改：只要回傳「格子」，parser 會處理排法。

## 換掉 LINE（新增發送方式）

1. 在 `messengers/` 新增類別，實作 `Messenger` protocol：`send`、`check`、`audience_size`、`quota`、`close`。
2. `config.MessengerSettings.kind` 加上名字；`messengers/__init__.py` 的 `build_messenger()` 和 `MESSENGER_KINDS_ZH` 各加一行。
3. 群組表的 `LINE_ID` 欄就是「收件者 ID」。換服務時，一起調整 `tables.LINE_ID_RE` 的格式驗證。

## 設計決策

| 決策 | 原因 |
|---|---|
| 對照表用 CSV | Excel／Numbers／Google Sheet 都能開，網頁也能改，非工程師看得懂 |
| 不用 line-bot-sdk | 只用到幾個 REST API；少一個大型依賴；錯誤訊息能完全翻成白話 |
| 排程放在程式裡（APScheduler） | 不用教使用者設定 Windows 工作排程器／cron；網頁能直接改時間；可以開機補發 |
| 伺服器端畫面（Jinja2），不用前端框架 | 沒有建置步驟、離線可用、好維護 |
| 機密只放 `.env`，由網頁寫入 | `settings.yaml` 可以安心備份；避免 Windows 記事本把 `.env` 存成 `.env.txt` |
| 防重複：SQLite 記錄（群組, 日期, 聚會）+ `X-Line-Retry-Key` | 排程重跑、補發、連點都不會重複；網路重試 LINE 也保證不重複 |
| 預覽不寫紀錄 | 首頁每次打開都會預覽，寫進資料庫只會一直變大 |
| `requirements.txt`、`.bat` 的變數部分只用 ASCII | 中文 Windows 的 pip 會用 cp950 讀 requirements；`.bat` 顯示中文靠 `chcp 65001` |

## 測試

```bash
pip install -r requirements-dev.txt
pytest
```

涵蓋：日期、三種排法、名字對照、訊息排版（含 textV2 跳脫）、規劃與所有問題偵測、CSV 表格（含 Big5）、
設定與 `.env`、LINE API（`httpx.MockTransport`：重試、409、額度用完、@ 失敗改純文字）、
整合測試（假的發送端：防重複、失敗通知、token 錯誤停止、額度警告）、Webhook 簽章與事件、網頁（TestClient）。

## 部署到別的地方（未來）

目前設計是「一台常開的電腦」。要改成雲端主機（不用電腦一直開）：

- 需要持久化的磁碟放 `config/` 和 `data/`
- `web.host` 改 `0.0.0.0`，並一定要設 `UI_PASSWORD`
- LINE Webhook 需要 HTTPS 網址

也可以不開網頁，只用系統排程每週執行一次 `python -m church_bot send`。

## 之後可以做的事

- 只私訊「當週有服事的人」（大幅節省 LINE 額度）
- Email 備援通知（LINE 本身壞掉時）
- 一個程式管理多份服事表／多個教會
- 網頁目前沒有 CSRF 保護：預設只開放本機（127.0.0.1）；開放區網時務必設定密碼
