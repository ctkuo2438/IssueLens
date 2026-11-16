# 驗證證據索引

這裡保存 M1／M2／M3／M5 的驗證紀錄、原始模型輸出、來源、審閱與畫面。功能與目前狀態以 [M1](../docs/m1.md)／[M2](../docs/m2.md)／[M3](../docs/m3.md)／[M5](../docs/m5.md) 為準；本索引協助找到證據，不把歷史結果改寫成最新驗收。

## 先看最新結果

M5 打包第三步讓 Claude Code 2.1.325 與 Codex 0.169.0 啟動用 `uvx` 從本機 repo 安裝的 `issuelens-mcp`，設定從 `~/.config/issuelens/.env` 讀取，各實際分析一次 `pallets/flask#6146`。兩者都使用 `gpt-5.6`／`medium`、列出完整報告且沒有執行指令；Claude Code 為 `completed`，Codex 因一次搜尋只讀了第一頁而為 `partial`。報告內容尚未經人工審閱。[驗收摘要](m5/verification/installed-live/20261115T205301Z/summary.json)

M5 打包第二步讓安裝版不依賴 checkout：環境變數優先，其餘讀 `~/.config/issuelens/.env`。探測顯示 Claude Code 會傳入 shell 的全部變數，Codex 預設只傳 `HOME`、`PATH` 等 9 個，所以改用家目錄下的檔案。以暫存 HOME 與假金鑰驗證安裝版的四種情況，沒有呼叫模型或 GitHub。[設定驗證](m5/verification/settings.json)

M5 打包第一步讓 IssueLens 可以建置成套件，並提供 `issuelens-mcp` 指令。wheel 只有 `backend/` 的 31 個檔案；在全新的暫存 HOME 用 `uvx` 安裝後完成握手並列出工具。沒有呼叫模型或 GitHub，金鑰讀取留待第二步。[打包驗證](m5/verification/packaging.json)

M5 Context 預算調整把 `AGENT_CONTEXT_TOKENS` 預設從 12,000 改為 24,000，並以真實 OpenAI 與 GitHub 重跑 `pallets/flask#6146`。結果從 `partial` 變成 `completed`，沒有省略任何摘錄，最新的維護者留言 S5 被引用；用量與 12,000 時幾乎相同。這次含預留空間的最大請求是 11,130 個 token，路徑與先前不同，所以不能說 12,000 一定失敗。報告內容尚未經人工審閱。[驗收摘要](m5/verification/context-budget-24000/20261113T220623Z/summary.json)

M5 Claude Code 真實驗收由使用者在 Claude Code 2.1.323 的互動工作階段分析同一個 `pallets/flask#6146`。工具呼叫約 22 秒完成，IssueLens 送出 14 則進度通知，Claude Code 收到的報告與 server 送出的完全相同；Claude 正確轉述 `partial` 報告與 8 個來源，沒有執行任何指令。這次是 auto 權限模式，沒有核准畫面；介面如何顯示進度尚未記錄，報告內容尚未經人工審閱。[驗收摘要](m5/verification/claude-code-live/20261113T202631Z/summary.json)

M5 Codex 真實驗收從 Codex 0.167.2 分析 `pallets/flask#6146`，使用真實的 OpenAI 與 GitHub。工具呼叫約 20 秒完成，IssueLens 送出 12 則進度通知，Codex 正確轉述 `partial` 報告與 7 個來源，沒有執行任何指令。`codex exec --json` 不顯示進度；報告內容尚未經人工審閱。[驗收摘要](m5/verification/codex-live/20261113T031334Z/summary.json)

M5 第一步的本機 MCP server 已和真實的 Claude Code 2.1.323、Codex 0.167.2 完成握手，兩者都取得 `analyze_github_issue`。Claude Code 先試 2026-07-28 的 `server/discover`，再退回 `2025-11-25`；Codex 直接使用 `2025-06-18`。兩個客戶端都使用暫存設定，沒有執行分析，也沒有呼叫模型或 GitHub。[客戶端連線驗證](m5/verification/client-handshake.json)

M3 無法讀取紀錄的驗收使用暫時的 PostgreSQL 叢集。一筆快照或 Trace 已無法通過驗證的排隊紀錄，修正前會讓 Worker 停止；修正後標為 `failed`，原始內容另存於 `incompatible_record`，之後排入的分析照常完成。啟動時遇到無法讀取的中斷紀錄也不再失敗。未呼叫模型或 GitHub。[無法讀取紀錄驗證](m3/verification/incompatible-records.json)

M3 資料庫重連驗收使用暫時的 PostgreSQL 叢集，未影響專案資料庫。以預設設定停機 48 秒，期間 API 回傳 503；資料庫恢復後 API 立即回到正常，Worker 在同一個程序內 3.1 秒後就緒。Worker 在分析執行中失去連線時，會重新取得鎖並從檢查點完成同一筆分析；其他儲存錯誤則停止 Worker。兩輪程式碼審查發現的問題與修正也記錄在內。未呼叫模型或 GitHub。[資料庫重連驗證](m3/verification/database-reconnect.json)

M3 Trace 驗收使用真實 PostgreSQL 與 Worker 程序。替換 Worker 後，同一任務 ID 可查到原嘗試、重試關聯與停止原因。工具呼叫途中中斷的測試也確認未保存的耗時留空。模型與 GitHub 回應使用模擬資料。[Trace 驗證](m3/verification/trace.json)

M3 有界重試驗收涵蓋模型與 GitHub 的暫時性失敗、預算耗盡和永久錯誤。真實 PostgreSQL 與 Worker 程序驗證替換後遵守已保存的重送時間，並以原任務 ID 完成。模型與 GitHub 回應使用模擬資料。[有界重試驗證](m3/verification/bounded-retry.json)

M3 Checkpoint 驗收用真實 PostgreSQL 與 Worker 程序，分別在標籤讀取途中正常停止與強制終止 Worker。替換程序接續已保存的模型與工具狀態，以相同任務 ID 完成兩次分析。模型與 GitHub 回應使用模擬資料。[Checkpoint 驗證](m3/verification/checkpoint.json)

M3 背景 Worker 驗收用真實程序與 PostgreSQL 強制重啟 API，同一個 Worker 持續執行分析，新的 API 可接續重播事件並讀回同一筆結果。分析內容使用模擬回應。[背景 Worker 驗證](m3/verification/background-worker.json)

M3 第一步以一筆真實模型與 GitHub 分析，核對重啟後原網址、完整 JSON、8 個來源與 35 筆 Activity 一致。[真實重啟驗收](m3/verification/live-persistence/20261110T220602_439770Z/report.json)／[重啟後畫面](m3/verification/live-persistence/20261110T220602_439770Z/after-restart.jpg)

當時的 280 項檢查與模擬分析另有紀錄。[第一步實作驗證](m3/verification/postgresql.json)

### M2 凍結驗收

M2 已凍結，以 `gpt-5.6`／`medium`、原預算對 HTTPX #3602、pytest #14560、Ruff #29177 各執行一次。三案都有最終答案，一案部分完成；依事先標準通過本輪有限功能驗收，使用者人工審閱仍待確認。

- [最新閱讀版：結果、限制與原始草稿](live-acceptance/20261110T065740_115528Z/review.md)
- [原始 API 執行報告](live-acceptance/20261110T065740_115528Z/report.json)／[助理來源核對](live-acceptance/20261110T065740_115528Z/assistant-review.json)
- [事先協議](live-acceptance/20261110T065740_115528Z/protocol.json)／[案例標準](live-acceptance/20261110T065740_115528Z/cases.json)／[獨立選材](live-acceptance/20261110T065740_115528Z/selection.json)

這輪沒有答案修正、範本或 README 查閱事件；不能將有限通過解讀成所有分支實測、穩定品質或生產準確率。

## 目錄怎麼分

```text
evidence/
  README.md
  m1/
    verification.json
    live-analysis.jpg
  m2/
    verification/
    screenshots/
  m3/
    verification/
  m5/
    verification/
  evaluations/
    <UTC timestamp>/
  live-acceptance/
    <UTC timestamp>/
```

| 位置 | 保存內容 | 如何判讀 |
| --- | --- | --- |
| [M1 驗證](m1/verification.json)／[畫面](m1/live-analysis.jpg) | 基本流程與單筆真實瀏覽器檢查。 | 功能連線觀察，不是品質基準。 |
| `m2/verification/` | 各開發步驟的檢查摘要、指紋及觀察。 | 可能同時記錄離線模擬與真實 API，依各檔說明區分。 |
| `m2/screenshots/` | 真實功能畫面與明確標示的離線畫面。 | 離線畫面不是模型品質證據。 |
| `m3/verification/` | PostgreSQL 保存與可靠性開發的檢查紀錄。 | 真實資料庫與模擬模型分開標示，不回填 M2 品質分數。 |
| `m5/verification/` | MCP server 與 Claude Code、Codex 的連線及真實分析紀錄。 | 客戶端整合的觀察，不是分析品質證據。 |
| `evaluations/` | 五筆固定虛構 GitHub 快照的真實模型執行，目前保留最新一輪。 | 不包含真實 GitHub 網路延遲，也不衡量搜尋召回率。 |
| `live-acceptance/` | 真實模型＋真實 GitHub 的驗收與審閱，目前保留 M2 凍結驗收。 | 執行完成不等於語意通過。 |

日期目錄使用 UTC；`20261110...` 的本機日期可能仍是 2026-11-09。`scripts/evaluate.py` 與 `scripts/acceptance.py` 仍寫到 `evaluations/` 與 `live-acceptance/` 的新日期目錄。

## 固定虛構快照的模型評估

| UTC 執行目錄 | 自動結果 | 觀察與入口 |
| --- | --- | --- |
| `20261109T083809_862702Z` | 5／5 | 範本功能後的回歸；固定資料沒有範本，不能驗收範本品質。[報告](evaluations/20261109T083809_862702Z/report.json) |

這是當時報告的自動清單符合案數，不是同尺度的品質分數，仍待使用者人工審閱。單次五案符合沒有關閉 PNG 案多推 `needs-info` 的波動；較早四輪的結果與共同規則重評見 [M2 歷史證據索引](../docs/m2.md#history)與[共同規則比較](m2/verification/behavior.json)。

## M2 開發驗證摘要

| 主題 | 紀錄 |
| --- | --- |
| 相似 Issue | [similar-issues.json](m2/verification/similar-issues.json) |
| 標籤推薦 | [labels.json](m2/verification/labels.json) |
| 五案評估程式 | [evaluation.json](m2/verification/evaluation.json) |
| 分類／重現歸因 | [behavior.json](m2/verification/behavior.json) |
| 範本 | [templates.json](m2/verification/templates.json) |
| 草稿維護者語氣 | [draft-style.json](m2/verification/draft-style.json) |
| 初版 Context Builder | [context.json](m2/verification/context.json) |
| 可編輯草稿 | [editable-draft.json](m2/verification/editable-draft.json) |
| 初次真實驗收 | [live-acceptance.json](m2/verification/live-acceptance.json) |
| 留言讀取 | [comments.json](m2/verification/comments.json) |
| PR 標籤用途 | [label-scope.json](m2/verification/label-scope.json) |
| 標籤文字一致性 | [label-consistency.json](m2/verification/label-consistency.json) |
| 引用／答案修正 | [answer-repair.json](m2/verification/answer-repair.json) |
| 草稿歷史歸因首輪／後續 | [draft-grounding.json](m2/verification/draft-grounding.json)／[draft-grounding-final.json](m2/verification/draft-grounding-final.json) |
| 最低必要查閱 | [required-lookups.json](m2/verification/required-lookups.json) |
| 草稿動作／關閉狀態 | [draft-status.json](m2/verification/draft-status.json) |
| 動作規則後真實回歸 | [draft-status-live.json](m2/verification/draft-status-live.json) |
| 推理設定 | [reasoning-settings.json](m2/verification/reasoning-settings.json) |

這些摘要是對應開發當時的測試數與程式指紋。凍結前最新 262 項測試／Pyright 紀錄見 [M2 檢查說明](../docs/m2.md#verification)，不將早期摘要改成新結果。

## 畫面

| 畫面 | 類型與來源 |
| --- | --- |
| [M1 基本分析](m1/live-analysis.jpg) | 真實 GitHub／模型功能檢查。 |
| [相似 Issue](m2/screenshots/live-comparison.jpg)／[標籤](m2/screenshots/live-labels.jpg)／[範本](m2/screenshots/live-templates.jpg) | 真實功能檢查畫面，非品質基準。 |
| [YAML 欄位](m2/screenshots/template-yaml-offline.jpg) | 虛構表單、離線元件檢查。 |
| [草稿桌面](m2/screenshots/editable-draft-offline.jpg)／[手機](m2/screenshots/editable-draft-mobile-offline.jpg) | 虛構資料、模擬模型與互動檢查。 |

## 保存方式

工作目錄只保留各里程碑**目前**的證據。2026-11-15 整理時刪除了已被後續結果取代的 80 個檔案：

- M2 開發期的十輪舊三案真實驗收（Flask #5804、#4032、Werkzeug #2948），保留最後的凍結驗收；
- 四輪較早的五案評估，保留最新一輪；
- 已結束的兩次模型實驗與它們的兩份摘要；
- 搬移對照用的 `manifest.json`，以及 `evaluation/` 中與凍結驗收逐位元組相同的三份協議、案例與選材。

這些檔案在 commit `abf218d` 仍完整存在，例如 `git show abf218d:evidence/manifest.json`，或用 `git restore --source abf218d -- evidence/model-comparisons` 取回整個目錄。各輪的重要觀察摘要在 [M2 歷史證據索引](../docs/m2.md#history)。保留的舊 JSON 可能記載已刪除或搬移前的路徑，作為當時紀錄原樣保留，不改寫內容。

之後新增證據：完整執行結果放日期目錄，手動驗證摘要與畫面放對應里程碑資料夾，再更新本索引。新的結果取代舊結果時，可以刪除舊的原始輸出，但要在相關文件留下觀察摘要，並註明可從哪個 commit 取回。不要把原始 JSON 合成一份、覆寫保留中的報告，或把人工編輯稿當成模型原稿。
