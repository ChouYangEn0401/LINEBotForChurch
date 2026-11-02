# 給程式呼叫的指令（ClawBot 的 `/line` 面板用的）

> 這一份是**對外契約**：旁邊的 ClawBot（Telegram）照這裡寫的欄位和結束碼接。改欄位要兩邊一起改。
> 一般使用者不用看這份：平常用管理網頁、雙擊 `scripts\windows\*.bat` 就好；`send`、`quota`、`狀態` 這些老指令見 [README「三種發送方式」](../README.md#三種發送方式挑方便的用)。
> 標「**預設，可改**」的是這一版先這樣定的，要換跟維護的人說。

## 怎麼呼叫

```
<專案>\.venv\Scripts\python.exe -m church_bot <指令> <動作> --json --by=telegram:<id> [--ministry=<編號或名稱>] [其他 --旗標=值]
```

環境：`PYTHONPATH=src`、`CHURCH_BOT_HOME=<專案根>`、工作目錄＝專案根（跟 ClawBot 呼叫 `webhook` 一樣）。
人在這台電腦上打也可以：`scripts\windows\cli.bat roster show`（不加 `--json` 就印給人看的字）。

| 規則 | 說明 |
|---|---|
| 值一律 `--旗標=值` 一個 token | 例如 `--date=2026-09-13`、`--from=小明`。分成兩個 token（`--date 2026-09-13`）也讀得懂，但呼叫的程式請用一個 |
| `--json` | stdout **最後一行**是一份 JSON（現在 stdout 也只有這一行；紀錄寫在 stderr 和 `data/church_bot.log`）。stdout 不是 UTF-8 時中文會用 `\uXXXX` 跳脫，JSON 解出來一樣 |
| 結束碼 | `0` 成功、`1` 沒做成（JSON 的 `message` 說原因，`hint` 說怎麼辦）、`2` 設定有問題（設定檔壞了、參數看不懂） |
| 失敗時的 JSON | 一律 `{"ok": false, "message": "…", "hint": "…", …}`；有些會多帶幫得上忙的欄位（見各指令的「沒做成」） |
| `--ministry=` | 編號（`m1`）或名稱（`青年牧區`，不分全半形、大小寫、空白）。**只有一個牧區時可以省**；兩個以上沒給 → 結束碼 1，`ministries: [{"id","name"}]` 列出有哪些；給了但找不到 → 結束碼 1，一樣列出 |
| `--by=` | 誰要做的，例如 `telegram:123456`（最多 80 字）。**寫入的指令（`swap person`／`weeks`／`undo`、`remind now`）沒給 → 結束碼 1**；看的指令可以不給 |
| 參數看不懂 | 結束碼 2；有 `--json` 的話照樣印 `{"ok": false, "message": "參數看不懂：…"}` |

### 什麼會花 LINE 額度

**只有 `remind now`（不加 `--dry-run`）和老的 `send`。** 其他全部不花：`roster show`／`image` 只是讀服事表、
`swap` 只寫這台電腦上的一個檔案、`quota` 是問 LINE「用了多少」（不算則數）、`ministries list` 只讀設定。
免費讓群組看到這週提醒的做法：在 LINE 群組打 `/提醒`（Reply 不算額度）。

---

## `roster show`：看服事表（上兩週＋下四週）

```
python -m church_bot roster show --json [--ministry=m1] [--weeks-back=2] [--weeks-forward=4]
```

範圍跟 LINE 的 `/行事曆` 一樣：以今天那一週的週日為基準，往前 `--weeks-back` 週（0–12，預設 2）、往後 `--weeks-forward` 週（1–26，預設 4），對齊到整週。
資料跟 `/提醒`、`/行事曆`、每週推播是同一條路（讀 Sheet → 解析 → **疊人員調換**）；每次都重新讀 Sheet（不用 60 秒快取）。

```json
{"ok": true, "ministry": {"id": "m1", "name": "青年牧區"}, "today": "2026-09-11",
 "window": {"start": "2026-08-23", "end": "2026-10-03", "weeks_back": 2, "weeks_forward": 4},
 "column": "服飾", "source": "Google Sheet：2026 服事表 / 9月",
 "overrides": {"active": 1, "applied": 1},
 "days": [{"date": "2026-09-13", "weekday": "日", "label": "主日崇拜", "note": "聖餐主日",
           "texts": [["服飾", "深色西裝"]],
           "assignments": [{"role": "司琴", "names": ["林美華"], "raw": ["美華"], "adjusted": true}],
           "adjusted": true, "adjusted_texts": [], "past": false, "this_week": false}]}
```

| 欄位 | 意思 |
|---|---|
| `today` | 這個牧區時區的今天 |
| `window` | 實際的範圍（含頭含尾） |
| `column` | 「照原樣顯示的欄位」裡填得最多的那一欄（就是 `/行事曆` 顯示的那一欄，例如服飾）；沒設定就是 `""` |
| `source` | 給人看的資料來源 |
| `overrides.active`／`applied` | 生效中的調換幾筆／這次真的套上幾筆（沒套上的原因看 `swap list`） |
| `days[].weekday` | `一`～`日` |
| `days[].texts` | 照原樣顯示的欄位 `[[欄位, 內容]…]`（空的不列） |
| `days[].assignments[].names` | 訊息上顯示的名字（對過同工名單；小團是團名）；`raw` 是服事表上的寫法 |
| `days[].assignments[].adjusted` | 這一項被調換過 |
| `days[].adjusted` | 這一天有任何調換（訊息會在這一天標「（已調整）」） |
| `days[].adjusted_texts` | 被調換過的照原樣顯示的欄位（例如 `["服飾"]`） |
| `past`／`this_week` | 已經過了／本週（週日～週六） |

沒做成：讀不到服事表 → 結束碼 1（`message` 是原因，例如 Google 沒開共用）；服事表來源沒設定 → 結束碼 2。

## `roster image`：畫成一張 PNG

```
python -m church_bot roster image --out=C:\…\roster.png --json [--ministry=m1] [--weeks-back=2] [--weeks-forward=4]
```

同一份資料畫成一張寬 1080px 的圖（手機一個螢幕寬）：一列一天，左邊日期＋星期＋聚會名，中間每一項服事和人（三格一排），
右邊「照原樣顯示的欄位」（服飾）。**被調換過的格子是黃底＋「※」**，本週那一列藍框，過去的灰掉，最下面一行寫圖例和資料來源。
只用 Pillow（不用 matplotlib、不用 AI、不用 LINE 額度）。資料夾不存在會自己建；先寫暫存檔再改名，對方不會讀到一半的檔。

```json
{"ok": true, "ministry": {"id": "m1", "name": "青年牧區"}, "path": "C:\\…\\roster.png", "days": 6,
 "font": "msjh", "today": "2026-09-11", "window": {…}, "column": "服飾"}
```

`font`：用了哪個中文字型（`msjh` 微軟正黑體 → `msyh` 微軟雅黑 → `mingliu` 細明體 → Mac/Linux 的 PingFang／Noto）；
**`"fallback"`＝都找不到，用了 Pillow 內建字型，中文會是方塊**，呼叫的人可以改傳文字版（`roster show`）。
沒給 `--out` → 參數看不懂（結束碼 2）。

## `swap`：人員調換（疊在服事表上，不改 Google Sheet）

### `swap person`：那一天那一項把某人換成另一人

```
python -m church_bot swap person --date=2026-09-13 --role=司琴 --from=小明 --to=美華 [--reason=出國] [--label=主日崇拜] --by=telegram:123 --json
```

- `--from` 寫服事表上的寫法或同工名單的正式名字都可以（「小明」「陳小明」都對得到）；記下來的是服事表上的寫法。
- `--role` 照服事表的寫法（不分全半形、大小寫、空白）。
- `--label`：同一天有兩場聚會時說是哪一場；只有一場可以省。
- 那一格有好幾個人時只換那一個（`喜樂、恩典` 換恩典 → `喜樂、建國`）。

```json
{"ok": true, "ministry": {…}, "id": "3fa2c1", "summary": "9/13（日） 主日崇拜 司琴：小明 → 美華",
 "item": {…同 swap list 的一筆…}, "message": "已調換：…"}
```

沒做成（結束碼 1）：

| 情況 | JSON 多帶 |
|---|---|
| `--from` 不在那一格 | `current: [那一格現在的名字]`，`message` 說那一格現在是誰 |
| 服事表上沒有那一天 | `nearby: [前後兩週有的日期]` |
| 那一天沒有這一項 | `roles: [那一天有的項目]` |
| 那一天有兩場聚會、沒給 `--label` | `labels: [聚會名]` |
| `--to` 已經在那一格、`--from` 跟 `--to` 是同一人 | — |
| 一模一樣的調換已經有了 | `existing: 編號` |
| 日期不是 YYYY-MM-DD | — |

### `swap weeks`：兩天對調

```
python -m church_bot swap weeks --a=2026-09-13 --b=2026-09-20 [--role=司琴 | --column=服飾] [--label-a=…] [--label-b=…] [--reason=…] --by=telegram:123 --json
```

| 給什麼 | 換什麼 |
|---|---|
| 都不給 | **整天**：所有服事項目＋照原樣顯示的欄位對調（日期、聚會名、備註不動） |
| `--role=司琴` | 只換那一項的人 |
| `--column=服飾` | 只換那一欄的文字 |

回的跟 `swap person` 一樣。沒做成（結束碼 1）：其中一天服事表上沒有（`nearby`）、那一天沒有這一項（`roles`）、
沒有這個欄位（`columns: [現有的照原樣顯示的欄位]`）、`--role` 和 `--column` 一起給、兩天這部分一模一樣、同一場聚會。

### `swap list`：看有哪些調換

```
python -m church_bot swap list --json [--all]
```

預設只列生效中的；`--all` 連「已退回」「已自動退掉」的也列。會讀一次服事表，順便看每一筆現在有沒有套上
（讀不到服事表也照樣列，`roster_error` 寫原因、`applied` 不出現）。

```json
{"ok": true, "ministry": {…}, "count": 1, "all": false, "roster_error": "",
 "items": [{"id": "3fa2c1", "kind": "person", "summary": "9/13（日） 主日崇拜 司琴：小明 → 美華",
            "by": "telegram:123", "at": "2026-09-11T20:00:00+08:00", "reason": "出國", "status": "active",
            "dates": ["2026-09-13"], "role": "司琴", "from": "小明", "to": "美華", "label": "主日崇拜",
            "applied": true, "note": ""}]}
```

| 欄位 | 意思 |
|---|---|
| `kind` | `person` 或 `weeks` |
| `status` | `active`（生效中）、`retired`（Sheet 已經改成一樣，自動退掉；有 `retired_at`）、`undone`（有人退回；有 `undone_at`、`undone_by`） |
| `dates` | 牽涉到的日期（person 一個、weeks 兩個） |
| `applied` | 生效中的這一筆**現在有沒有套上**；`false` 時 `note` 說為什麼（例如「那一格現在是『建國』，已經不是『小明』」） |
| `role`／`column`／`from`／`to`／`label`／`label_a`／`label_b` | 有值才出現 |

### `swap undo`：退回一筆

```
python -m church_bot swap undo --id=3fa2c1 --by=telegram:123 --json
```

→ `{"ok": true, "id", "summary", "status": "undone", "message"}`。不刪掉那一筆，只標 `undone`（查得到誰、什麼時候退的）。
找不到這個編號、或那一筆已經不是生效中 → 結束碼 1。

### 覆蓋層怎麼運作（為什麼不寫回 Google Sheet）

- **存在哪**：`data/ministries/<牧區編號>/overrides.json`，一筆一個調換：`id`、`kind`、`at`（何時）、`by`（誰）、`reason`、`status` 和內容。
  寫檔先寫暫存檔再改名、讀改寫整段跨程式排隊（管理網頁和指令列同時寫也不會壞）；每次寫入都進**變更紀錄**
  （管理網頁「變更紀錄」頁看得到、可以還原；來源寫「Telegram（ClawBot）」或「指令列」——變更紀錄刻意只記來源，是誰寫在那一筆的 `by`）。
- **為什麼不寫回 Sheet**：讀 Sheet 的兩種方式（公開連結、服務帳號當檢視者）都是唯讀；要寫回得給編輯權限，改錯就是改到正本。
  覆蓋層改錯了 `swap undo` 就回來，Sheet 一個字都沒動。（之後要不要改成寫回，等擁有者決定。）
- **套在哪**：只有一個地方——讀服事表的 `BotService.fetch_roster`（parser 之後、所有用服事表的地方之前）。
  所以**每週推播、LINE 的 `/提醒`、`/行事曆`、`/點名`、管理網頁的預覽、`preview`／`send`、`roster`** 看到的都是調換後的。
  管理網頁「服事表」頁的**原始格子**刻意不疊（那一頁是讓人看 Sheet 本身長什麼樣子）。
- **訊息上**：有調換的那一天，日期後面標「（已調整）」**一次**（模板沒用到日期的話，最後補一行「🔁（已調整）…」），不是每個名字都標。
  `/行事曆` 那一列多一個「已調整」。內容變了，所以那一週的提醒會被當成「改過」再送一次（跟 Sheet 改了一樣）。
- **每次套用時看三種情況**（照建立的順序一筆一筆疊）：
  1. Sheet 已經是調換後的樣子（有人把 Sheet 改好了）→ 那一筆**自動標 `retired`**、寫回檔案，之後不再套。
  2. Sheet 還是建立調換時的樣子 → 套用。
  3. 都不是（那一格又被改成別人了）→ **這次不套，照 Sheet**；那一筆還是 `active`，`swap list` 的 `applied: false` 寫原因。
     不自動退掉（預設，可改）：可能只是暫時改的，讓人決定要 undo 還是留著。
  比對時名字不分全半形、大小寫、空白；`swap person` 比的是那一格裡有沒有 `--from`／`--to`，
  `swap weeks` 比的是兩天「那一部分」的內容（整天／那一項／那一欄）跟建立時記下的一不一樣。
- **Sheet 寫的是別的寫法**（調換寫「林美華」，Sheet 改成「美華」）：比不出是同一人，會落在第 3 種（不套、照 Sheet，結果一樣是美華），
  `swap undo` 收掉就好。
- 檔案壞了（被手動改壞）：**提醒照常發**，當作沒有調換，紀錄檔寫一行錯誤；要寫入新的調換時會拒絕（免得蓋掉），到「變更紀錄」還原。
- 過去的調換不會自己消失（`swap list` 一直列著，預設，可改）；不想看到就 `swap undo`。

## `remind now`：現在就把這週的提醒推到 LINE 群組（**花 LINE 額度**）

```
python -m church_bot remind now --by=telegram:123 --json [--ministry=m1] [--target=<群組名或 LINE ID>] [--dry-run]
```

就是 `send --force` 只對這個牧區（同一個 `BotService.run`：讀服事表＋疊調換 → 規劃 → 防重複的鎖 → Push → 記錄 → 通知管理員）。
`--force`：這週已經送過也照送。`--target`：只送這一個群組（要是這個牧區「LINE 群組」頁裡**啟用中**的）。
`--dry-run`：**不送**，只回會送幾則、哪些群組、大約用掉幾則額度——ClawBot 問「這會用掉 LINE 的 N 則額度，要送嗎？」就是先跑這個。
測試模式（發送方式＝測試模式）下照樣算成功，訊息寫到 `data/ministries/<id>/outbox.log`，`messages_used` 是 0。

```json
{"ok": true, "dry_run": false, "test_mode": false, "ministry": {…},
 "sent": 2, "planned": 2, "failed": 0,
 "groups": [{"name": "同工群", "ok": true, "error": "", "status": "sent", "messages": 1, "audience": 35}],
 "messages_used": 47, "messages_exact": true,
 "quota": {…見 quota…}, "issues": [{"severity": "warning", "message": "…", "hint": "…"}],
 "message": "已送出 2 則到 2 個群組，大約用掉 LINE 47 則額度"}
```

| 欄位 | 意思 |
|---|---|
| `sent` | **真的送出**的則數（`--dry-run` 時是 0） |
| `planned` | 這次要送（或 `--dry-run` 時會送）的則數（一個群組、一場聚會一則） |
| `failed` | 送失敗的則數 |
| `groups[]` | 每個群組：`ok`（沒有失敗）、`error`（第一個失敗的原因）、`status`（`sent`／`dry_run`／`failed`）、`messages`（幾則）、`audience`（群組人數；LINE 按人數算額度；不知道是 `null`） |
| `messages_used` | 大約用掉（或會用掉）幾則 LINE 額度＝每則 × 群組人數。**不含**發完傳給管理員的彙報／警告（設定裡開了才有，每位管理員 1 則） |
| `messages_exact` | `false`＝有群組問不到人數，`messages_used` 只算了問得到的 |
| `quota` | 本月用量（同 `quota --json` 的欄位，沒有 `ok`）；測試模式或關了額度檢查是 `null` |
| `issues` | 這次要人處理的事（警告、錯誤），跟管理網頁看到的一樣 |

沒做成（結束碼 1，`ok: false`）：這幾天服事表沒有要提醒的內容（`planned: 0`）、有任何一則沒送出、讀不到服事表、
`--target` 不是啟用中的群組（`groups: [啟用中的群組名]`）、這個牧區還沒有會收到提醒的群組。
LINE 群組 ID 不會出現在輸出裡（只有群組名）。

## `quota --json`：本月 LINE 用量

```
python -m church_bot quota --json [--force]
```

整個 LINE 帳號一份（所有牧區共用），所以沒有 `--ministry`。該問的時候才問 LINE（上次查到的不到 15 分鐘就直接用）；`--force` 一定重問。

```json
{"ok": true, "enabled": true, "used": 37, "limit": 200, "remaining": 163, "month": "2026-09",
 "checked_at": "2026-09-11T20:05:00+08:00", "pending": false, "error": "",
 "describe": "本月已用 37 / 200 則（剩 163 則）", "status": "剛剛更新", "message": "本月已用 37 / 200 則（剩 163 則）"}
```

`limit: null`＝無上限方案。`pending: true`＝剛發送完，LINE 的統計再幾分鐘才算進去。`error` 有字＝這次問不到（數字是上次的），結束碼 1。
每個牧區都是測試模式、或關了額度檢查 → `{"ok": true, "enabled": false, "message": "…"}`，結束碼 0。

## `ministries list --json`：有哪些牧區

```
python -m church_bot ministries list --json      （或 ministries --json、牧區 --json）
```

回一個**清單**（不是物件）：

```json
[{"id": "m1", "name": "青年牧區", "enabled": true, "day_of_week": "thu", "time": "20:00",
  "every_n_weeks": 1, "schedule": "每星期四 20:00", "next_send": "2026-09-17T20:00:00+08:00", "groups": 2, "error": ""}]
```

`enabled`＝自動發送開著；`next_send` 是下一次自動發送（關著是 `null`）；`groups`＝會收到提醒的群組數；
那個牧區的設定檔壞了 → `enabled: false`、`error` 寫原因（不會整個指令失敗）。

---

## 跟路線圖（ClawBot `docs/plans/2026-11-02-line-admin-service.md` 第 3 節）不一樣的地方

| 那裡寫 | 這裡做成 | 為什麼 |
|---|---|---|
| `swap list` 回 `[…]` | 回 `{"ok", "items": […], …}` | 跟其他指令一樣有 `ok`／`message`，失敗時才分得出來 |
| `remind now` 的 `sent` 在 `--dry-run` 時是「會送幾則」 | `sent` 一律是真的送出的；會送幾則看 `planned` | 一個欄位只有一個意思，ClawBot 不會把試算當成已送 |
| `roster image` 回 `{"ok", "path", "days"}` | 多 `font`、`today`、`window`、`column` | 字型找不到時看得出來 |
| `quota --json` 回 `{"ok","used","limit","month","status"}` | 多 `enabled`、`remaining`、`checked_at`、`pending`、`error`、`describe` | 現有快照本來就有這些欄位 |
| `ministries list --json` 回 `{"id","name","enabled","next_send"}` | 多 `day_of_week`、`time`、`every_n_weeks`、`schedule`、`groups`、`error` | 照現有 `牧區` 指令算得出的 |
| `swap person`／`weeks` | 多 `--label`／`--label-a`／`--label-b` | 同一天兩場聚會時分得開 |
