# 版本紀錄

## 未發佈

- 文件：新增交接文件 [docs/HANDOVER.md](docs/HANDOVER.md)
- 文件：新增「LINE 的 ID、群組成員與私訊」運作原理說明 [docs/LINE_IDS_AND_MEMBERS.md](docs/LINE_IDS_AND_MEMBERS.md)

## 0.1.0 — 2026-09-11

第一版（MVP）。

- 資料來源：Google Sheet（公開連結、服務帳號）、本機 CSV
- 自動判斷三種服事表排法；日期、名字的寫法都很寬鬆（民國年、缺年份、合併儲存格、一格多人…）
- 群組表、人員表：CSV 檔，可以用 Excel 或管理網頁編輯
- LINE Messaging API：重試不重複（X-Line-Retry-Key）、@ 標記（textV2）失敗自動改送純文字、發送前檢查本月額度
- 每週自動排程、開機補發、防重複發送
- 問題分三級（錯誤／提醒事項／參考資訊），顯示在首頁並用 LINE 通知管理員
- 管理網頁、JSON API（`/docs`）、LINE Webhook（「群組ID」「我的ID」指令、被加入群組自動登記、被踢出群組通知管理員）
- Windows `.bat` 與 macOS `.sh` 腳本：安裝、啟動、健康檢查、立即發送、開機自動執行
- 文件：安裝設定教學、服事表格式、LINE 方案與費用整理（報帳用）、架構說明
