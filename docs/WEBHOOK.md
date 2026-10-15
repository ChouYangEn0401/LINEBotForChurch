# LINE 指令（webhook）怎麼運作

> 給：伺服器管理員，和之後接手維護這台電腦的人。2026-10-15 擁有者定的做法；問過的問題都在最後的「常見問題」。

## 一句話

**後台一直開著；LINE 指令要用才開，開幾個小時自己關。** 每週提醒是後台發的，跟 LINE 指令開不開沒有關係。

## 兩支程式、兩個 Windows 服務、兩個小圖示

| | 後台 | LINE 指令（webhook） |
|---|---|---|
| Windows 服務名稱 | `church-bot` | `church-bot-webhook` |
| 做什麼 | 管理網頁（127.0.0.1:8787）＋每週自動發提醒 | 開一條 Cloudflare 臨時網址，自動登記成 LINE 的 Webhook。開著的時候群組打 `/提醒`、`/我的名字` 機器人才會回，外面的人也才連得到管理網頁（還是要密碼） |
| 開機 | **自己起來**（延遲自動啟動） | **不會自己起來**（手動啟動） |
| 當掉 | nssm 10 秒後自己再開 | nssm 10 秒後自己再開，「開到幾點」照舊 |
| 什麼時候停 | 不停（要停用小圖示或 `2-service-stop.bat`） | 開滿設定的小時數（預設 3）自己停；也可以隨時關 |
| 右下角小圖示 | **L** | **Lw** |
| 誰來開關 | 不用管它 | 你：小圖示、`.bat`、Telegram（ClawBot） |

兩個服務都是 [nssm](https://nssm.cc) 包起來的，這就是最終的做法（不交給 CloudServicesPlatform 管）。

### 小圖示是什麼

右下角的 **L**、**Lw** 是兩個很小的程式（`scripts\windows\tray.pyw`，沒有黑色視窗），登入 Windows 時自己出現。
它們只負責「看服務在不在、讓你按開關」，**關掉小圖示不會停掉服務**，服務停了小圖示也不會消失。
顏色：藍＝服務在跑、綠＝雙擊開的黑色視窗在跑、黃＝正在啟動／停止或沒回應、灰＝沒在跑。

**服務在背景跑，看不到任何黑色視窗。** 只有你雙擊 `2-start.bat`／`3-open-webhook.bat` 的時候才會有黑色視窗（那是不用服務的用法）。

## 第一次在新電腦上裝

1. 雙擊 `scripts\windows\1-install.bat`（建 `.venv`、裝套件）。
2. 照 [SETUP_LINE.md](SETUP_LINE.md) 填好 `.env` 的 LINE 金鑰；要用 LINE 指令的話先裝 cloudflared（`winget install --id Cloudflare.cloudflared`）。
3. 雙擊 `scripts\windows\service\install.bat`（跳一次「是否允許」，按「是」）。它會：
   - 把兩個服務都註冊好，啟動／停止的權限開給你的 Windows 帳號（之後開關不會再跳 UAC）；
   - **啟動後台**；**LINE 指令註冊好、先停著**（要用才開）；
   - 在「啟動」資料夾放小圖示的捷徑，並把小圖示打開。

裝完：後台在跑（L 是藍的）、LINE 指令停著（Lw 是灰的）、沒有任何黑色視窗。

## 要用 LINE 指令的時候

任選一種：

| 在哪裡 | 怎麼做 |
|---|---|
| 這台電腦 | 右鍵 **Lw** →「▶ 開 webhook（3 小時後自己關）」；或雙擊 `scripts\windows\3-service-start.bat` |
| 手機（Telegram） | ClawBot 打 `/line_bot_on`（要先 `/sudo`）；`/line_bot_on 2` 開 2 小時；`/line_bot` 看狀態、`/line_bot_off` 關 |
| 指令列 | `scripts\windows\cli.bat webhook on --hours 2`、`webhook off`、`webhook status`（加 `--json` 印一行 JSON） |

開了之後會發生的事：

1. 建一條臨時網址、自動登記到 LINE（大約半分鐘到一分鐘），Telegram 收到「🟢 LINE webhook 開好了」和網址。
2. 已經開著再按一次「開」＝**延長**，從現在起算。
3. 關掉前 10 分鐘，Telegram 收到「⏰ 再 10 分鐘自動關」。
4. 時間到自己關，Telegram 收到「⏹ 已關閉（時間到）」。Lw 變灰。每週提醒照常。

## 設定（`config/church.yaml` 的 `webhook:`，整個教會一份）

```yaml
webhook:
  hours: 3                    # 開多久自己關；0 = 不自動關
  warn_minutes: 10            # 關掉前幾分鐘預告；0 = 不預告
  dev_notice: notifier_tb     # 通知伺服器管理員：notifier_tb（Telegram）/ command（執行自己的程式）/ off
  dev_command: []             # dev_notice: command 時執行的程式，例如 ["C:/tools/notify.exe", "{text}"]
  line_admins_on_open: false  # 開好時，LINE 私訊每位牧區管理員（每人 1 則額度）
  line_admins_on_close: false # 快關／已關時，LINE 私訊每位牧區管理員
```

沒寫這一段就全部用上面的預設。改完不用重開：下一次開 webhook 就照新的。

## 通知誰

- **伺服器管理員（你）**：開好、延長、快關、已關，都經這台電腦的 Notifier_TB 傳 Telegram。不佔 LINE 額度。
  不用 Telegram 的人把 `dev_notice` 改成 `command`：每次都執行 `dev_command` 一次，`{event}`、`{text}`、`{until}`、`{url}`
  會換成這次的內容，環境變數 `CHURCH_BOT_EVENT_JSON` 是整份事件。
- **牧區管理員**：LINE 私訊，兩個開關**預設關**（LINE 額度現在不夠；以後買了額度再開）。
  打開 `line_admins_on_close` 的話，快關時大家會收到：「再 10 分鐘…後台將停止訪問。需要繼續使用的話，請聯絡伺服器管理員（周暘恩）。每週的服事提醒照常會自動發送。」
- 「請聯絡○○」的名字：同工名單裡勾了「**伺服器管理員**」那一欄的人。只有伺服器管理員（管理者登入）能勾。

## 程式更新之後要做什麼（不用重新安裝）

| 改到什麼 | 要做什麼 |
|---|---|
| 程式（`src/`） | 後台：右鍵 **L** →「服務：重新啟動」。LINE 指令：下一次開就是新的程式（開著的話右鍵 Lw →「服務：重新啟動」） |
| 小圖示本身（`tray.py`） | 登出再登入 Windows（小圖示登入時才重開）；或用工作管理員結束兩個 `pythonw.exe`（tray.pyw）再雙擊 `scripts\windows\tray.pyw` |
| 服務的設定（`service.ps1` 的 nssm 設定） | 才需要重跑 `service\install.bat`。例：2026-10-15 把 webhook 改成手動啟動，已經用 `sc.exe config church-bot-webhook start= demand` 改好了，不用重裝 |

## 現在這台電腦的狀態（2026-10-15 晚上）

- 後台 `church-bot`：在跑、開機自動啟動、23:13 重開過，跑的是新程式。
- LINE 指令 `church-bot-webhook`：停著，已改成手動啟動（DEMAND_START）。
- 同工名單：周暘恩 已勾成伺服器管理員。
- `church.yaml` 沒有 `webhook:` 這一段，用預設（3 小時、10 分鐘預告、Telegram 通知、不傳 LINE）。

## 常見問題

**新電腦上裝了 webhook 那支程式，會自己打開嗎？會有一個視窗一直在嗎？**
不會自己開（手動啟動），也沒有視窗。只有右下角的 Lw 小圖示一直在，它是灰的，等你按「▶ 開 webhook」。

**webhook 需要裝嗎？**
只有要讓群組打指令（`/提醒`、`/我的名字`、`/點名`），或要從外面進管理網頁時才需要。只要每週提醒的話，不裝 cloudflared 也可以（install.bat 會只註冊後台）。

**改完程式要重新安裝嗎？**
不用。看上面的「程式更新之後要做什麼」。

**小圖示選單裡找不到「啟動 webhook」？**
在 **Lw**（不是 L）的選單裡。2026-10-15 以前那一項叫「服務：啟動（nssm start）」，之後改成「▶ 開 webhook（3 小時後自己關）」。新的字要等小圖示重開（登出再登入）才會出現。

**時間到自己關，跟我按「關」有什麼不同？**
沒有不同，都是停掉 `church-bot-webhook` 這個服務。差別只在通知上寫的原因（「時間到」或「telegram 關的」）。

**Telegram 開，跟電腦上開，是同一個東西嗎？**
是。ClawBot 的 `/line_bot_on` 就是呼叫 `cli.bat webhook on`，啟動的是同一個服務。ClawBot 只能開關 webhook，管不到後台。
