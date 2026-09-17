# ⛪ 教會服事表 LINE 自動提醒機器人

每週固定時間，自動讀 **Google Sheet 服事表**，把「這週誰服事什麼」發到 **LINE 群組**。
有任何問題（服事表沒更新、名字對不到、LINE 送不出去）都會**通知管理員**，不會默默失敗。

```
📣 本週服事提醒
📅 9/13（週日）・主日崇拜

▸ 講員：王大衛牧師
▸ 司會：吳恩典
▸ 敬拜主領：陳小明
▸ 司琴：林美華
▸ 招待：黃喜樂、吳恩典

📝 聖餐主日
謝謝大家的擺上 🙏
```

---

## 它會幫你做什麼

- 📄 **讀服事表**：直接讀 Google Sheet；三種常見排法都認得，日期寫法（9/13、9月13日、115/9/13…）也很寬鬆。
- 👥 **分群組發送**：例如「敬拜團」群組只收敬拜相關的服事。
- 🙋 **整理名字**：服事表寫綽號，訊息顯示正式名字；名字對不到會猜「可能是誰」；已經停用的人又被排到會提醒。
- ⏰ **自動發送**：每週固定時間發；電腦剛好關機錯過，12 小時內打開程式會自動補發；同一次聚會不會重複發。
- 🚨 **出問題一定讓你知道**：管理網頁首頁顯示紅／黃色提醒，同時用 LINE 通知管理員。
- 🖥️ **管理網頁**：不用改程式、不用打指令，用瀏覽器點一點就能設定。

---

## 需要準備

| 項目 | 說明 | 費用 |
|---|---|---|
| 一台常開的電腦（Windows 或 Mac） | 程式在這台電腦上執行 | — |
| Python 3.11 以上 | 安裝程式會自動檢查，沒有會告訴你去哪裡下載 | 免費 |
| LINE 官方帳號（Messaging API） | 步驟見 [docs/SETUP_LINE.md](docs/SETUP_LINE.md) | 免費方案每月 200 則，詳見 [docs/LINE_PRICING.md](docs/LINE_PRICING.md) |
| Google Sheet 服事表 | 步驟見 [docs/SETUP_GOOGLE.md](docs/SETUP_GOOGLE.md) | 免費 |

---

## 安裝（只要做一次）

### Windows

1. 安裝 Python：到 <https://www.python.org/downloads/> 下載安裝。**安裝的第一個畫面記得勾「Add python.exe to PATH」**。
2. 打開 `scripts\windows` 資料夾，**雙擊 `1-install.bat`**，等它跑完。
3. **雙擊 `2-start.bat`**，瀏覽器會自動打開管理網頁 <http://127.0.0.1:8787>。

> 如果跳出「Windows 已保護您的電腦」：按「其他資訊」→「仍要執行」。

### Mac

1. 安裝 Python 3.11 以上：到 <https://www.python.org/downloads/macos/> 下載安裝。
2. 打開「終端機」（按 ⌘ + 空白鍵，搜尋「終端機」或 Terminal）。
3. 輸入 `bash`（後面打一個空格），把 `scripts/mac/1-install.sh` **拖進終端機視窗**，按 Enter。
4. 用同樣方法執行 `scripts/mac/2-start.sh`，瀏覽器會打開管理網頁 <http://127.0.0.1:8787>。

> **`2-start` 的視窗要一直開著**，每週才會自動提醒。想要開機自動執行，看下面第 6 步。

---

## 第一次設定（跟著做，大約 30 分鐘）

安裝完一打開，程式先用**範例服事表**，首頁就能看到訊息長什麼樣子。接著：

> 🔰 第一次做、或想先確定不會出錯再動教會的群組？改看 **[docs/QUICKSTART_TEST.md](docs/QUICKSTART_TEST.md)**——
> 一樣的步驟，但會先教你只測試自己一個人，最後才接觸教會正式的群組。

1. **申請 LINE 機器人**：照 [docs/SETUP_LINE.md](docs/SETUP_LINE.md) 做，把拿到的 token 貼到網頁「⚙️ 設定 → 🔑 LINE 金鑰」。
2. **接上服事表**：照 [docs/SETUP_GOOGLE.md](docs/SETUP_GOOGLE.md) 把 Google Sheet 開成「知道連結的人可以檢視」，網址貼到「⚙️ 設定 → ① 服事表從哪裡來」。服事表怎麼排見 [docs/SHEET_FORMAT.md](docs/SHEET_FORMAT.md)。
3. **設定群組**：把機器人邀進 LINE 群組，在群組打「/群組ID」，到「👥 群組」新增群組、貼上 ID，按「測試」確認收得到。
4. **設定管理員**：私訊機器人「/我的ID」，把 ID 填到「⚙️ 設定 → ④ 出問題時通知誰」。
5. **系統檢查**：打開「🩺 系統檢查」，全部 ✅ 就完成了！
6. **（建議）開機自動執行**：Windows 雙擊 `scripts\windows\autostart-on.bat`；Mac 執行 `scripts/mac/autostart-on.sh`。

---

## 平常怎麼用

**什麼都不用做。** 只要照常更新 Google Sheet 上的服事表就好。

| 想做的事 | 去管理網頁的哪裡 |
|---|---|
| 看這週會發什麼 | 🏠 首頁 |
| 臨時想提早發、服事表改了想重發 | 🏠 首頁的「立刻發送」／「重新發送」 |
| 新增群組、群組換了 | 👥 群組 |
| 新同工、有人離開、名字對不到 | 🙋 人員 |
| 改發送時間、訊息長相 | ⚙️ 設定 |
| 看以前發了什麼、有沒有失敗 | 📜 紀錄 |
| 覺得怪怪的 | 🩺 系統檢查 |

也可以雙擊 `check.bat`／`check.sh`（健康檢查＋預覽）或 `send-now.bat`／`send-now.sh`（立刻發送）。

---

## 各種狀況，程式會怎麼處理

| 狀況 | 程式會怎樣 | 你要做什麼 |
|---|---|---|
| 服事表出現新名字、打錯字 | 照原樣送出，並提醒管理員（附上「可能是誰」） | 到「🙋 人員」一鍵處理 |
| 有人離開教會／不再服事 | — | 「🙋 人員」改成停用，以後被排到會提醒 |
| 服事表快用完 | 剩 14 天時提醒管理員 | 排下一期 |
| 這週服事表沒資料 | **不發送**，❌ 通知管理員 | 更新服事表後按「立刻發送」 |
| 機器人被踢出群組 | 通知管理員 | 重新邀請，或把群組停用 |
| LINE token 失效 | ❌ 通知管理員、首頁變紅 | 重新發行 token（見 SETUP_LINE） |
| LINE 本月額度用完 | ❌ 通知管理員 | 見 [docs/LINE_PRICING.md](docs/LINE_PRICING.md) |
| 電腦剛好關機錯過時間 | 12 小時內打開程式自動補發 | 建議設定開機自動執行 |
| 同一週按了兩次發送 | 第二次自動略過 | 真的要重發請按「重新發送」 |
| 服事表某一列日期寫錯 | 那一列跳過，首頁告訴你是第幾列 | 修正那一列 |

> ⚠️ 如果問題出在 LINE 本身（例如 token 失效），就沒辦法用 LINE 通知你了。這時問題只會顯示在管理網頁首頁和 `data/church_bot.log`，建議偶爾打開首頁看一眼。

---

## 費用

- LINE：詳見 **[docs/LINE_PRICING.md](docs/LINE_PRICING.md)**（有報帳需要的發票、扣款資訊）。一個 30 人的群組每週提醒一次，免費方案就夠用。
- 注意：**推播到群組是按群組人數計算則數**；**2026/11/1 起 LINE 付費方案調價**。
- 其他（程式、Google Sheet）全部免費。

---

## 檔案說明

| 檔案 | 是什麼 | 可以手動改嗎 |
|---|---|---|
| `config/settings.yaml` | 一般設定 | 可以，但建議用網頁改 |
| `config/targets.csv` | 群組表 | 可以，用 Excel 開就能改 |
| `config/members.csv` | 人員表 | 可以，用 Excel 開就能改 |
| `.env` | LINE 金鑰、網頁密碼（**機密**） | 建議用網頁改 |
| `data/church_bot.log` | 紀錄檔；出問題時把它傳給維護的人 | 不用 |
| `data/church_bot.db` | 發送紀錄（用來防止重複發送） | 不用；刪掉會忘記送過什麼 |

> 🔒 `.env`、`settings.yaml`、`targets.csv`、`members.csv`、`service-account.json` 都設定成**不會上傳到 git**，範例檔（`*.example.*`）才會。

---

## 文件總覽

| 文件 | 內容 |
|---|---|
| [docs/QUICKSTART_TEST.md](docs/QUICKSTART_TEST.md) | **新手推薦從這裡開始**：從零申請帳號、安全測試（只有自己看得到），最後才接上教會正式群組 |
| [docs/SETUP_LINE.md](docs/SETUP_LINE.md) | LINE 機器人申請與設定（完整參考） |
| [docs/SETUP_GOOGLE.md](docs/SETUP_GOOGLE.md) | 接上 Google Sheet 服事表 |
| [docs/SHEET_FORMAT.md](docs/SHEET_FORMAT.md) | 服事表怎麼排、讀不到時怎麼改 |
| [docs/LINE_PRICING.md](docs/LINE_PRICING.md) | LINE 方案、費用、用量、發票（報帳用） |
| [docs/LINE_IDS_AND_MEMBERS.md](docs/LINE_IDS_AND_MEMBERS.md) | 群組 ID、個人 ID 怎麼來；能不能知道群組有哪些人；能不能私訊、怎麼算錢 |
| [docs/HANDOVER.md](docs/HANDOVER.md) | 交接：目前狀態、已驗證／未驗證、上線清單、帳號與機密、待確認事項 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 程式架構（給工程師） |

---

## 給工程師

- 技術：Python 3.11+、FastAPI + Jinja2（伺服器端畫面，無前端建置）、APScheduler、httpx、gspread、pydantic v2、SQLite。
- 指令（`PYTHONPATH=src`）：`python -m church_bot init | web | check | preview | send [--force]`
- 測試：`pip install -r requirements-dev.txt`，然後 `pytest`
- JSON API：管理網頁開著時看 <http://127.0.0.1:8787/docs>
- 架構、錯誤處理原則、**怎麼把 LINE／Google Sheet 換成別的服務**：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 版本紀錄：[CHANGELOG.md](CHANGELOG.md)

---

## 授權

[MIT License](LICENSE)：任何教會或個人都可以自由使用、修改、再散布，只要保留授權聲明。
