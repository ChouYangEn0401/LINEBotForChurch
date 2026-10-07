# 架構說明（給維護的工程師）

## 一次執行的流程

```
            ┌─ sources/ ───────────────────────┐
 Google     │ RosterSource.fetch() → RawSheet  │   只負責「拿到格子」（全部字串）
 Sheet/CSV  └──────────────────────────────────┘
                 │
                 ▼ core/parser.py      自動判斷 wide / long / matrix → Roster（日期 × 服事 × 人）
                 ▼ core/planner.py     挑出這次的聚會、依 LINE 群組產生訊息、找出所有問題（Issue）
                 │    ├ core/directory.py  名字 → 同工名單（別名、全形半形、括號註記、猜測）
                 │    └ core/renderer.py   Jinja2 模板 → 純文字 + LINE textV2（@ 人）
                 ▼ core/dispatcher.py  防重複（SQLite）→ 檢查額度 → Messenger.send() → 每則結果
            ┌─ messengers/ ────────────────────┐
            │ LineMessenger / ConsoleMessenger │   只負責「把字送出去」
            └──────────────────────────────────┘
                 ▼ service.py          記錄 RunReport、通知管理員
```

`service.BotService` 是一個牧區的組裝點：它只看自己資料夾裡的設定和名單（`config.Paths(root, ministry)`），
完全不知道有別的牧區。`ministries.Church` 管「有哪些牧區」，每個牧區一個 BotService（共用一份教會資料庫）；
`web/`、`scheduler.py`、`webhook.py`、`cli.py` 都透過 Church 拿到要處理的那個牧區。多牧區的設計見 [MINISTRIES.md](MINISTRIES.md)。
每次執行都重新讀設定與 CSV，所以改完設定、改完 Excel 不用重開程式。

## 目錄

| 檔案 | 職責 |
|---|---|
| `config.py` | `settings.yaml`（pydantic 驗證）+ `.env`（機密）；路徑（`Paths(root, ministry)`：牧區的資料夾、整個教會共用的檔案） |
| `church.py` | 牧區清單 `config/church.yaml`：新增、改名、移除、牧區密碼（PBKDF2）；從單一牧區自動搬家 |
| `ministries.py` | 執行中的整個教會：每個牧區一個 BotService；群組屬於哪個牧區、人跟哪些牧區有關、還沒分配的群組 |
| `files.py` | 所有設定與名單的寫檔（先寫暫存檔再改名），順便留變更紀錄 |
| `models.py` | 純資料：Roster、ServiceDay、Target、Member、Team、Issue、RunReport… |
| `errors.py` | `ChurchBotError(message, hint)` 與子類別 |
| `tables.py` | LINE 群組、同工名單、小團的 CSV 讀寫（編碼自動判斷、逐列驗證） |
| `sources/` | 資料來源：`csv_file`、`google_public`（免金鑰）、`google_service_account` |
| `core/dates.py` | 日期解析（民國年、缺年份推測、Excel 序號…）與格式化 |
| `core/parser.py` | 表格 → Roster，三種排法自動判斷 |
| `core/directory.py` | 名字對照（一人多名；服事表寫小團名稱就展開成團員） |
| `core/renderer.py` | 訊息排版（Jinja2、textV2 mention） |
| `core/planner.py` | 決定發給誰、發什麼；產生所有 Issue |
| `core/dispatcher.py` | 防重複、額度檢查、發送、失敗處理 |
| `core/history.py` | SQLite：執行紀錄、發送紀錄、Webhook 看到的群組與人（LINE 名稱、本人登記的名字與暱稱、群組成員關係）、跨程式共用的小狀態（`state` 表）；舊資料庫開啟時自動補欄位 |
| `core/accounts.py` | LINE 帳號 ↔ 同工名單的對應狀態（已對應／改名／對應／衝突／加入）、待確認的暱稱 |
| `core/quota.py` | 本月 LINE 用量的快照：什麼時候該再問 LINE、問不到時留住舊數字（主控台那一格、`/api/quota`、`cli.bat quota` 共用） |
| `core/message_template.py` | 罐頭訊息的【中文標籤】寫法：標籤、【服事名單】一項一行、沒資料就拿掉（舊的 Jinja2 寫法在 renderer 照樣能用） |
| `core/totp.py` | 驗證器 App 的 6 位數（RFC 6238，標準函式庫），QR code 用 segno（沒裝就顯示金鑰） |
| `core/login_codes.py` | Telegram 一次性登入碼（照 CSP：碼＋頁面上的 nonce、90 秒、錯 3 次作廢、速率限制） |
| `core/versions.py` | 變更紀錄：內容定址（SHA-256、zlib、相同內容只存一份）、來源、摘要、差異、還原；直接用 Excel 改的也抓得到 |
| `core/public_url.py` | 目前對外的網址（免費模式每次重開都會變）：從 Webhook 請求的 Host 認出來，供 LINE 指令「/服務網址」回覆 |
| `messengers/` | 發送端：`line`（Messaging API）、`console`（測試模式） |
| `service.py` | 組裝一次完整執行、管理員通知、健康檢查 |
| `scheduler.py` | APScheduler：每個牧區一個每週鬧鐘 + 開機補發；`due_on` 給 `cli send` 判斷今天輪到誰 |
| `webhook.py` | LINE Webhook：依群組分到牧區；「/」開頭的聊天指令（`parse_command`）、加入群組、被踢出群組、被動收集 LINE 帳號 |
| `remote_config.py` | 「/設定」可以改的項目與一次性驗證碼（安全設計寫在檔案開頭） |
| `web/` | FastAPI：`app.py` 是教會這一層（首頁＝所有牧區、全教會設定、登入），`ministry.py` 是 `/m/<編號>/…` 牧區的後台，`manager.py` 是伺服器管理員登入，`common.py` 是共用零件與三種身分（訪客、牧區管理員、伺服器管理員）；管理網頁（Jinja2，`templates/_macros.html` 是共用零件）+ `/api/*`、`/m/<編號>/api/*` JSON API；`web/overview.py` 是主控台的「運作流程」和「問題 → 去哪一頁處理」；`/m/<編號>/api/nav` 是側欄每一項現在的狀況（首頁也拿它顯示每個牧區的狀況）；`static/roster.js` 把服事表畫成表格；樣式的顏色只從 `static/app.css` 開頭那組變數來 |
| `cli.py` | `python -m church_bot init / web / check / preview / send / quota / 牧區 / set-webhook / tunnel`（`--牧區` 指定牧區）；`web` 是外層程式＋子程式（`--child`），子程式用代碼 3 結束就重開（「重新啟動」按鈕） |

## 錯誤處理原則：不要沉默

1. 可預期的錯誤一律丟 `ChurchBotError` 子類別，帶 `message`（發生什麼）和 `hint`（下一步怎麼做）。
2. 一次執行的所有狀況都收集成 `Issue`（ERROR／WARNING／INFO），放進 `RunReport`。
3. `RunReport` 同時出現在：網頁主控台、執行紀錄（SQLite）、log 檔、CLI 結束代碼、管理員 LINE 通知（ERROR、WARNING）。
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
3. LINE 群組的 `LINE_ID` 欄就是「收件者 ID」。換服務時，一起調整 `tables.LINE_ID_RE` 的格式驗證。

## 設計決策

| 決策 | 原因 |
|---|---|
| 對照表用 CSV | Excel／Numbers／Google Sheet 都能開，網頁也能改，非工程師看得懂 |
| 不用 line-bot-sdk | 只用到幾個 REST API；少一個大型依賴；錯誤訊息能完全翻成白話 |
| 一個牧區一個資料夾 | 原本的程式只要換資料夾就能用；搬移、備份、刪除一個牧區就是一個資料夾；牧區之間名單互不相通 |
| 變更紀錄自己做，不用 git／DoltLite | 不用另外裝東西、Windows 能用、CSV 照樣能用 Excel 改；網頁上看得到差異、一鍵還原 |
| 排程放在程式裡（APScheduler） | 不用教使用者設定 Windows 工作排程器／cron；網頁能直接改時間；可以開機補發。已經有常駐的 Telegram 機器人時，改由它呼叫 `cli.bat send`（重試、失敗跳視窗見 cli.py），內建排程可以關掉 |
| 伺服器端畫面（Jinja2），不用前端框架 | 沒有建置步驟、離線可用、好維護 |
| 機密只放 `.env`，由網頁寫入 | `settings.yaml` 可以安心備份；避免 Windows 記事本把 `.env` 存成 `.env.txt` |
| 防重複：SQLite 記錄（群組, 日期, 聚會）+ `X-Line-Retry-Key` | 排程重跑、補發、連點都不會重複；網路重試 LINE 也保證不重複 |
| 預覽不寫紀錄 | 主控台每次打開都會預覽，寫進資料庫只會一直變大 |
| `requirements.txt`、`.bat` 的變數部分只用 ASCII | 中文 Windows 的 pip 會用 cp950 讀 requirements；`.bat` 顯示中文靠 `chcp 65001` |

## 測試

```bash
pip install -r requirements-dev.txt
pytest
```

涵蓋：日期、三種排法、名字對照、訊息排版（含 textV2 跳脫）、規劃與所有問題偵測、CSV 表格（含 Big5）、
設定與 `.env`、LINE API（`httpx.MockTransport`：重試、409、額度用完、@ 失敗改純文字）、
整合測試（假的發送端：防重複、失敗通知、token 錯誤停止、額度警告）、Webhook 簽章與事件、聊天指令解析、
LINE 帳號對應、「/設定」驗證碼（過期、錯三次、只限本人、每日上限）、資料庫升級、網頁（TestClient）、
多牧區（搬家、群組分流、每個牧區的鬧鐘、`cli send` 今天輪到誰）、三種身分與伺服器管理員登入（本機／外面、
驗證器 RFC 測試向量、Telegram 碼的 nonce 與作廢）、重新啟動、每個群組自己的訊息、【中文標籤】跟舊預設排出來一模一樣、變更紀錄與還原。
大部分測試在 m1「測試牧區」的資料夾裡跑（`conftest.paths`），跟實際使用一樣；`client` 的網址預設在 `/m/m1/` 裡面。
LINE Webhook 的假物件在 `tests/line_fakes.py`（放在 conftest 會被 pytest 載入成兩份，有狀態的假物件會對不上）。

## 部署到別的地方（未來）

目前設計是「一台常開的電腦」。要改成雲端主機（不用電腦一直開）：

- 需要持久化的磁碟放 `config/` 和 `data/`
- `web.host` 改 `0.0.0.0`，並一定要設 `UI_PASSWORD`
- LINE Webhook 需要 HTTPS 網址

也可以不開網頁，只用系統排程每週執行一次 `python -m church_bot send`。

## 之後可以做的事

- 只私訊「當週有服事的人」（大幅節省 LINE 額度）
- Email 備援通知（LINE 本身壞掉時）
- 每個人一個網頁帳號、牧區管理員只看自己的牧區；用量按牧區統計（見 [MINISTRIES.md](MINISTRIES.md)「之後可以做的」）
- 網頁目前沒有 CSRF 保護：預設只開放本機（127.0.0.1）；開放區網時務必設定密碼
