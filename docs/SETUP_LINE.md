# LINE 機器人設定（第一次做一次就好）

> LINE 的後台偶爾改版，按鈕名稱可能和這裡寫的不完全一樣，照意思找就可以。
> 以前常用的 **LINE Notify 已在 2025/3/31 停止服務**，現在要用「LINE 官方帳號 + Messaging API」。
> 費用與額度見 [LINE_PRICING.md](LINE_PRICING.md)。

## 0. 先搞懂：其實是「兩個網站」，這是最容易卡關的地方

| 網站 | 網址開頭 | 拿什麼 / 改什麼 |
|---|---|---|
| **LINE Developers**（技術後台） | `developers.line.biz/console/...` | Channel access token、Channel secret、Webhook URL、按 Verify、開 Use webhook |
| **LINE Official Account Manager**（官方帳號後台，簡稱 OA Manager） | `manager.line.biz/account/...` | 建立官方帳號、頭像介紹、回應設定（聊天模式、自動回應）、帳號設定（加入群組權限） |

兩個網站要**登入同一個 LINE 帳號**，但畫面、選單完全不一樣，同一件事有時兩邊都有入口（例如 Webhook 開關兩邊都能看到，但實際生效、要填網址的地方是 Developers 那邊）。
下面每一步會註明是在哪個網站。網址裡的 `channel/一串數字` 是你這個 Channel 的內部編號、`@xxxxxxx` 是機器人的官方帳號 ID（加好友用的那個），這兩個都不是密碼，不用特別記，但也不用貼給別人看。

整個流程：

```
建立 LINE 官方帳號 → 開啟 Messaging API → 拿 token 貼到管理網頁
→ 調整官方帳號設定 → 把機器人邀進群組、拿到群組 ID → 貼到管理網頁
```

---

## 1. 建立 LINE 官方帳號

1. 用電腦打開 LINE Official Account Manager：<https://manager.line.biz/>，用教會管理用的 LINE 帳號登入。
2. 按「建立 LINE 官方帳號」，照畫面填：
   - 帳號名稱：例如「○○教會服事小幫手」（這就是機器人在 LINE 上的名字）
   - 業種、地區等照實填
3. 建好後，方案預設是免費的「輕用量」，不用改。

## 2. 開啟 Messaging API

1. 在 LINE Official Account Manager 裡點進剛建立的帳號 → 右上角「設定」→ 左邊選單「Messaging API」。
2. 按「啟用 Messaging API」。
3. 第一次會要你選「服務提供者（Provider）」：選「建立新的服務提供者」，名稱填教會名稱即可。
4. 隱私權政策、服務條款的網址可以留空，按確定。

## 3. 拿到 Channel access token（最重要）

1. 打開 LINE Developers：<https://developers.line.biz/console/>，用同一個 LINE 帳號登入。
2. 點剛剛建立的 Provider → 點你的官方帳號（Channel）。
3. 切到「**Messaging API**」分頁，拉到最下面「Channel access token (long-lived)」→ 按「**Issue**」。
4. 複製那一長串，打開管理網頁「⚙️ 設定」→ 最下面「🔑 LINE 金鑰與網頁密碼」→ 貼到 **Channel access token** → 按「儲存金鑰」。
5. （要用「/群組ID」指令才需要）切到「**Basic settings**」分頁，複製 **Channel secret**，貼到同一個地方。

> 🔒 token 就是機器人的鑰匙，**不要貼到群組或任何公開的地方**。萬一外洩，到同一個地方按「Reissue」重新發行，再貼一次新的。

## 4. 調整官方帳號設定（很重要）

不改的話，機器人**進不了群組**，或在群組裡**每句話都自動回覆**。
在 LINE Official Account Manager →「設定」：

| 位置 | 改成 | 為什麼 |
|---|---|---|
| 帳號設定 →「功能切換」→ 加入群組或多人聊天室 | **接受邀請加入群組或多人聊天室** | 不開的話機器人進不了群組 |
| 回應設定 → 聊天 | 關閉 | 避免切到手動聊天模式 |
| 回應設定 → **自動回應訊息** | **關閉** | 不然群組裡每句話它都會回「感謝您的訊息」 |
| 回應設定 → **Webhook** | 開啟 | 要用「/」開頭的聊天指令、自動收集 LINE 帳號才需要 |
| 回應設定 → 加入好友的歡迎訊息 | 隨意 | — |

> 這裡的「Webhook」開關和下一步（LINE Developers）的「Use webhook」是**同一件事的兩個入口**，兩邊都要是開的；
> 網址、Verify、Channel secret 這些**只有 LINE Developers 那邊**有，OA Manager 這裡沒有。

## 5. 把機器人加進群組，拿到群組 ID

**先加機器人好友**：LINE Developers「Messaging API」分頁上有 QR code，用手機 LINE 掃描加好友。
**再邀進群組**：打開要收提醒的群組 → 右上選單 → 邀請 → 從好友名單選機器人。

LINE 沒有地方能直接看到「群組 ID」，要讓機器人「聽到」群組裡的訊息才知道。兩種方法擇一：

> 想了解原理（ID 是怎麼來的、能不能知道群組有哪些人、能不能私訊）請看 [LINE_IDS_AND_MEMBERS.md](LINE_IDS_AND_MEMBERS.md)。

### 方法 A：用本程式內建的 Webhook（推薦）

機器人會直接在群組回覆 ID，而且**自動幫你加到「👥 LINE 群組」頁**。需要一個暫時的公開網址，這裡用免費的 Cloudflare Tunnel：

> ⚠️ **這個網址開著的時候，等於把整個管理網頁（不只是 Webhook）短暫公開到網路上**——
> 任何拿到這個網址的人都能打開你的管理網頁。如果你還沒在「⚙️ 設定」最下面設定「網頁密碼」，
> 建議先設一次（一次性動作，之後每次開 tunnel 都有效），或者至少做到「抓完 ID 就馬上關掉 tunnel」（第 7 步）。
> trycloudflare 給的網址是隨機的、沒有登記在任何地方，只要不貼到公開群組或論壇，通常不會被別人撞到。

1. 安裝 cloudflared（只要裝一次）
   - Windows：開「命令提示字元」，輸入 `winget install --id Cloudflare.cloudflared`
   - Mac：在終端機輸入 `brew install cloudflared`
2. 確認管理網頁開著（`2-start`），**另外開一個**命令視窗，輸入：
   ```
   cloudflared tunnel --url http://localhost:8787
   ```
3. 畫面會出現一個 `https://xxxx-xxxx.trycloudflare.com` 的網址，把它複製起來。
4. 回到 LINE Developers →「Messaging API」分頁 → Webhook settings：
   - **Webhook URL** 填：`https://xxxx-xxxx.trycloudflare.com/line/webhook`（後面要加 `/line/webhook`）
   - 按「**Verify**」，出現 Success 就對了（失敗的話：確認第 3 步的 Channel secret 有貼到管理網頁）
   - 打開「**Use webhook**」
5. 機器人被邀進群組時，會自己在群組說出群組 ID；已經在群組裡的話，在群組打 **`/群組ID`**。
6. 拿自己的 ID：私訊機器人 **`/我的ID`**（填到「⚙️ 設定 → ④ 出問題時通知誰」）。
7. 拿完 ID 後，可以把 cloudflared 視窗關掉（按 Ctrl + C）。**每週提醒不需要 Webhook**。
   以後要再抓新群組的 ID，重做第 2～4 步（每次的網址都不一樣，要重新貼）。
8. （選用）想一次收集大家的真實姓名：到「🙋 同工名單 → LINE 帳號」按「開放登記」，請大家在群組打 **`/我的名字 真實姓名`**，
   **收集的這段期間 cloudflared 要一直開著**；收完按「關閉登記」再關掉 cloudflared。開著的時間比較長，**請一定先設「網頁密碼」**。
   其他指令（`/設定` 等）見管理網頁「❓ 說明 → 在 LINE 可以打的指令」。

### 方法 B：用 webhook.site 看原始資料（不用安裝任何東西）

1. 打開 <https://webhook.site>，複製頁面上的「Your unique URL」。
2. 貼到 LINE Developers 的 Webhook URL，打開「Use webhook」。
3. 在群組裡隨便打一句話，回到 webhook.site，看剛收到的資料：`"groupId": "C……"` 那一串就是群組 ID，`"userId": "U……"` 是發話人的 ID。
4. ⚠️ **拿到 ID 後立刻把 Webhook URL 刪掉，或關掉 Use webhook**。開著的期間，群組訊息會被送到 webhook.site 這個第三方網站。

## 6. 設定到管理網頁

1. 「👥 LINE 群組」→ 新增：名稱隨便取、LINE_ID 貼 **C 開頭**那串、勾「啟用」→ 儲存 → 按「**測試**」，群組應該會收到一則測試訊息。
2. 「⚙️ 設定 → ④ 出問題時通知誰」貼 **U 開頭**那串（自己的 ID）。**管理員要先加機器人好友**，才收得到通知。
3. 打開「❓ 說明 → 🩺 系統檢查」，確認都是 ✅。

---

## 常見問題

| 狀況 | 檢查 |
|---|---|
| 邀請時找不到機器人 | 要先加機器人好友；第 4 步「接受邀請加入群組」有沒有開 |
| 群組收不到提醒 | 機器人還在群組嗎？LINE_ID 是 C 開頭嗎？「❓ 說明 → 🩺 系統檢查」的 LINE 連線是 ✅ 嗎？ |
| 管理員收不到通知 | 管理員有沒有加機器人好友、有沒有封鎖它 |
| 機器人在群組一直自動回話 | 關掉「自動回應訊息」（第 4 步） |
| Webhook 按 Verify 失敗 | 管理網頁要開著、cloudflared 要開著、Channel secret 要貼對、網址後面要有 `/line/webhook` |
| 打「/群組ID」沒反應 | Webhook 有沒有開、網址是不是舊的（cloudflared 每次重開網址都會變） |
| 想在訊息裡 @ 服事的人 | 同工名單要填每個人的 LINE_userId（請他在群組打「/我的ID」），LINE 群組勾「@ 標記服事的人」 |
