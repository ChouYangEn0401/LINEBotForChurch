# 交接文件

> 最後更新：2026-09-11（v0.1.0）
> 給：接手維護這個程式的人，以及負責的小組長。
> 怎麼使用請先看 [README](../README.md)；這份文件記錄「目前的狀態」和「交接時要注意的事」。

---

## 1. 專案現況

- 版本 **v0.1.0**（MVP）。git 分支 `main`，標籤 `v0.1.0`，**沒有推到任何遠端**（GitHub 等）。
- 已完成的功能見 [CHANGELOG](../CHANGELOG.md)。
- **還沒接上真的 LINE 帳號和真的服事表**：目前用範例服事表與「測試模式」驗證流程（見第 3 節）。

## 2. 已經驗證過的

| 項目 | 怎麼驗證的 |
|---|---|
| 核心邏輯 | 自動測試全部通過（`pytest`；2026-09-17 為 201 個）：日期、三種排法、名字對照、訊息排版、規劃與問題偵測、CSV 表格、設定、LINE API（模擬）、整合流程、Webhook、網頁 |
| Windows 安裝 | 在 Windows 10（Python 3.11.9）實際執行 `1-install.bat` 成功，中文顯示正常 |
| 網頁伺服器 | 實際啟動（uvicorn + 排程），各頁面正常；重複啟動會提示「已經在執行」 |
| 管理網頁操作 | 新增群組、錯誤 ID 被擋下、名字對應、設定驗證（錯誤時間、模板打錯字）、發送、防重複發送 |
| `check.bat`／`check.sh` | 分別用 cmd 與 bash 實際執行過 |
| Windows 開機自動執行 | 捷徑建立的指令在暫存資料夾測試過（沒有動到電腦真的「啟動」資料夾） |

## 3. 還沒驗證的（上線前要做）

| 項目 | 為什麼還沒驗證 | 怎麼驗證 |
|---|---|---|
| 真的 LINE 發送 | 還沒有 token | 設定 token →「👥 LINE 群組」按「測試」 |
| 真的 Google Sheet | 還沒有服事表網址 | 貼上網址 →「❓ 說明 → 🩺 系統檢查」看「服事表」那一列 |
| 教會實際的服事表格式 | 還沒拿到 | 首頁預覽的內容是否正確 |
| Mac 腳本 | 只做過語法檢查、在 Windows 的 Git Bash 執行過 | 在 Mac 上跑 `1-install.sh`、`2-start.sh` |
| Mac 開機自動執行（launchd） | 手邊沒有 Mac | 執行 `autostart-on.sh` 後重新開機 |
| LINE 後台的畫面步驟 | 依官方文件撰寫，畫面可能改版 | 照 [SETUP_LINE.md](SETUP_LINE.md) 實際做一遍，順手更正文件 |
| LINE 方案價格 | 是 2026-09-11 查的資料，**11/1 起調價** | 付款前再看一次官方頁面 |

## 4. 上線檢查清單

- [ ] 教會那台常開的電腦安裝好（Windows：`1-install.bat`；Mac：`1-install.sh`）
- [ ] LINE 官方帳號、Messaging API 開好，token 貼到「設定 → LINE 金鑰」（[SETUP_LINE.md](SETUP_LINE.md)）
- [ ] 官方帳號設定：接受邀請加入群組、**關閉自動回應訊息**
- [ ] 服事表開公開連結、網址貼到設定（[SETUP_GOOGLE.md](SETUP_GOOGLE.md)），首頁預覽內容正確
- [ ] 群組加好，按「測試」有收到
- [ ] 管理員加機器人好友，自己的 ID 填到設定
- [ ] 「❓ 說明 → 🩺 系統檢查」全部 ✅
- [ ] 算一下每月則數夠不夠（[LINE_PRICING.md](LINE_PRICING.md) 第 3 節）
- [ ] 設定開機自動執行（`autostart-on`）
- [ ] 第一次排程時間過後，到「📜 發送紀錄」確認有自動發送

## 5. 還需要教會確認的事

1. **實際的服事表**（截圖，或換成假名字的副本）→ 確認程式讀得正確。
2. **有幾個群組、每個群組大約幾個人** → 決定 LINE 免費方案（每月 200 則）夠不夠。
3. **有沒有一台會一直開著的電腦** → 沒有的話，要考慮放到雲端主機。
4. **要不要在訊息裡 @ 服事的人** → 要的話，每個人要在群組打一次「/我的ID」。
5. **要不要改成「只私訊當週服事的人」** → 比較省額度，但要開發新功能，而且每個人都要加機器人好友（見 [LINE_IDS_AND_MEMBERS.md](LINE_IDS_AND_MEMBERS.md)）。

## 6. 帳號、機密與要交接的東西

| 東西 | 在哪裡 | 交接時要做的事 |
|---|---|---|
| LINE 官方帳號 | LINE Official Account Manager | 至少設兩位管理員（「設定」裡的「權限管理」），不要只綁在一個人的 LINE 帳號上 |
| LINE Developers 的 Provider | developers.line.biz | 同上，也加入第二位管理員 |
| Channel access token、Channel secret | 那台電腦的 `.env` | 不要用 LINE／Email 傳送；換人時可以按「Reissue」重新發行，舊的就失效 |
| 服事表 Google Sheet | Google 雲端硬碟 | 擁有者最好是教會的共用帳號，不是個人帳號 |
| `service-account.json`（有用服務帳號才有） | `config/` | 跟 token 一樣，當作密碼保管 |
| 網頁密碼（有設才有） | `.env` 的 `UI_PASSWORD` | 交接時當面告知 |
| 付款信用卡、發票抬頭與統編 | LINE 官方帳號後台（帳務相關設定） | 見 [LINE_PRICING.md](LINE_PRICING.md) 第 4 節 |

## 7. 日常維護

- **每週**：不用做事。建議偶爾打開首頁看一眼（因為 LINE 本身出問題時，沒辦法用 LINE 通知你）。
- **每月**：看「❓ 說明 → 🩺 系統檢查」的 LINE 本月額度。
- **每季**：排好下一期服事表（程式會在剩 14 天時提醒）。
- **更新程式**：拿到新版（`git pull` 或直接覆蓋檔案）後，再執行一次 `1-install`，不會覆蓋任何設定。
- **備份**：`config/` 底下的 `settings.yaml`、`targets.csv`、`members.csv`（有用實驗功能的話加上 `org.csv`），還有 `.env`（機密，另外保管）。
  有在收集 LINE 帳號的話也備份 `data/church_bot.db`（刪掉會忘記送過什麼、收集到但還沒對應的 LINE 帳號；已經對應到同工名單的不受影響）。
- **出問題**：把 `data/church_bot.log` 傳給維護的人。

## 8. 已知限制

- 電腦關機時不會發送（開機後 12 小時內會補發）。
- LINE token 失效時，連管理員通知也送不出去，只能看網頁或 log。
- 免費的一般 LINE 帳號**拿不到群組完整成員名單**（見 [LINE_IDS_AND_MEMBERS.md](LINE_IDS_AND_MEMBERS.md)）。
- 管理網頁沒有 CSRF 防護：預設只開放這台電腦；要開放給其他電腦時，務必設定密碼。
- 一個程式只管一份服事表。

## 9. 之後可以做的功能

- 只私訊當週服事的人（省 LINE 額度）
- Email 備援通知（LINE 本身壞掉時）
- 放到雲端主機（電腦不用一直開）
- 大教會：牧區／小組分流、各自的管理員與服事表（見 [LAB_LARGE_CHURCH.md](LAB_LARGE_CHURCH.md)）

## 10. 開發與版本控制

- 程式架構、怎麼替換 LINE／Google Sheet：[ARCHITECTURE.md](ARCHITECTURE.md)
- 測試：`pip install -r requirements-dev.txt`，然後 `pytest`
- 每次修改：更新 [CHANGELOG](../CHANGELOG.md) → commit；發新版時 `git tag vX.Y.Z`
- 設定、名單、金鑰都**不會**進 git（`.gitignore` 已設定），只有 `*.example.*` 範例檔會進。
