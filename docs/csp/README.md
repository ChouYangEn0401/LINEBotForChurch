# CSP 交接文件（副本）

這個資料夾裡的檔案是 **CloudServicesPlatform 發布的副本**，由 CSP 的 `tools/artifacts/distribute.py` 從線上正本產生。

| 檔案 | 版本 | 線上正本（要改內容改這裡） |
| --- | --- | --- |
| [`architecture-report-2026-10-26.html`](architecture-report-2026-10-26.html) | rev 32 · 匯出 2026-10-27 · `22d8c349e4fcf4b5` | <https://claude.ai/artifact/VDtW9QV6RuabWpmi2Gr2r7> |

- **不要直接改這裡的 HTML**：改線上正本，然後在 CSP 的資料夾跑 `python tools/artifacts/distribute.py distribute`，
  所有專案的副本會一起更新；`python tools/artifacts/distribute.py status` 會列出哪個專案的副本過期。
- 每個 HTML 的 `<meta name="csp-artifact">` 是版本戳（正本 rev、匯出日、內容摘要），`status` 就是比這個。
- 發布清單：CSP 的 `docs/artifacts/distribution.toml`。
