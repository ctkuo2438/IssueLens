# 驗證證據索引

這裡保存 M1／M2／M3 的驗證紀錄、原始模型輸出、來源、審閱與畫面。功能與目前狀態以 [M1](../docs/m1.md)／[M2](../docs/m2.md)／[M3](../docs/m3.md) 為準；本索引協助找到證據，不把歷史結果改寫成最新驗收。

## 先看最新結果

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
  manifest.json
  m1/
    verification.json
    live-analysis.jpg
  m2/
    verification/
    screenshots/
  m3/
    verification/
  evaluations/
    <UTC timestamp>/
  live-acceptance/
    <UTC timestamp>/
  model-comparisons/
    <UTC timestamp>/
```

| 位置 | 保存內容 | 如何判讀 |
| --- | --- | --- |
| [M1 驗證](m1/verification.json)／[畫面](m1/live-analysis.jpg) | 基本流程與單筆真實瀏覽器檢查。 | 功能連線觀察，不是品質基準。 |
| `m2/verification/` | 各開發步驟的檢查摘要、指紋及觀察。 | 可能同時記錄離線模擬與真實 API，依各檔說明區分。 |
| `m2/screenshots/` | 真實功能畫面與明確標示的離線畫面。 | 離線畫面不是模型品質證據。 |
| `m3/verification/` | PostgreSQL 保存與可靠性開發的檢查紀錄。 | 真實資料庫與模擬模型分開標示，不回填 M2 品質分數。 |
| `evaluations/` | 五筆固定虛構 GitHub 快照的真實模型執行。 | 不包含真實 GitHub 網路延遲，也不衡量搜尋召回率。 |
| `live-acceptance/` | 真實模型＋真實 GitHub 的逐次驗收與審閱。 | 保留所有失敗／部分結果；執行完成不等於語意通過。 |
| `model-comparisons/` | 已結束的固定來源模型實驗。 | 專用程式已移除，不再作為待執行流程。 |

日期目錄使用 UTC；`20261110...` 的本機日期可能仍是 2026-11-09。`evaluations/`、`live-acceptance/`、`model-comparisons/` 原位置保留，現有 runner 的輸出路徑沒有變動。

## M2 真實 GitHub 驗收歷程

前十輪是同一批 Pallets 開發案例：Flask #5804、#4032、Werkzeug #2948。最後一輪才改用三個不同 repo 的新案例；完成後這三筆也已成為看過的案例。

| UTC 執行目錄 | 當時階段與主要觀察 | 閱讀入口 |
| --- | --- | --- |
| `20261109T212458_183451Z` | 初次三案：留言漏讀、PR 專用標籤及文字矛盾。 | [審閱](live-acceptance/20261109T212458_183451Z/review.md) |
| `20261109T215427_768430Z` | 留言補強：取得討論，但不代表運用正確。 | [審閱](live-acceptance/20261109T215427_768430Z/review.md) |
| `20261109T220345_892233Z` | PR 標籤用途補強；其他語意問題保留。 | [審閱](live-acceptance/20261109T220345_892233Z/review.md) |
| `20261109T221126_201141Z` | 一致性修正發生；另一案修正輸入超限，沒有答案。 | [審閱](live-acceptance/20261109T221126_201141Z/review.md) |
| `20261109T222509_737957Z` | 同證據重新生成：三案有答案，#4032 真實一致性修正。 | [審閱](live-acceptance/20261109T222509_737957Z/review.md) |
| `20261109T234613_939464Z` | 草稿歷史歸因首輪：#5804 耗盡請求，無最終答案。 | [審閱](live-acceptance/20261109T234613_939464Z/review.md) |
| `20261109T235404_357915Z` | 保留修正輪數後仍有時態與查閱覆蓋問題。 | [審閱](live-acceptance/20261109T235404_357915Z/review.md) |
| `20261110T003707_981988Z` | 最低查閱通過，草稿仍有不當動作／解決推論；另存人工參考稿。 | [審閱](live-acceptance/20261110T003707_981988Z/review.md)／[編輯稿](live-acceptance/20261110T003707_981988Z/edited-drafts.md) |
| `20261110T031609_643746Z` | 動作檢查後：#5804 輸入停止、#2948 標籤被動句漏判。 | [審閱](live-acceptance/20261110T031609_643746Z/review.md) |
| `20261110T053008_813104Z` | gpt-5.6、事前預留與被動句補強後：#4032 修正狀態衝突，無答案。 | [審閱](live-acceptance/20261110T053008_813104Z/review.md) |
| `20261110T065740_115528Z` | 凍結後的新三案：依事先標準有限通過，一案部分完成。 | [最新審閱](live-acceptance/20261110T065740_115528Z/review.md) |

每個目錄的 `report.json` 是原始執行報告；`assistant-review.json`／`review.md` 是另外保存的助理判讀，不替代原模型輸出。使用者人工狀態維持原記錄。

## 固定虛構快照的模型評估

以下數字是**當時報告的自動清單符合案數**，檢查規則曾變更，不能直接當成同尺度品質進步。兩份早期基準以共同新規則重評後各三案符合；原報告分數不改寫。全部仍待使用者人工審閱。

| UTC 執行目錄 | 原始自動結果 | 觀察與入口 |
| --- | --- | --- |
| `20261109T051034_358768Z` | 4／5 | 首次基準，另發現第一人稱重現宣稱。[報告](evaluations/20261109T051034_358768Z/report.json) |
| `20261109T053353_585136Z` | 3／5 | 分類及額外標籤未符合參考。[報告](evaluations/20261109T053353_585136Z/report.json) |
| `20261109T055654_100989Z` | 5／5 | 行為規則後單次觀察，仍有語意細節。[報告](evaluations/20261109T055654_100989Z/report.json) |
| `20261109T071019_500148Z` | 4／5 | PNG 再次多推 `needs-info`。[報告](evaluations/20261109T071019_500148Z/report.json) |
| `20261109T083809_862702Z` | 5／5 | 範本功能後回歸；固定資料沒有範本，不能驗收範本品質。[報告](evaluations/20261109T083809_862702Z/report.json) |

[共同規則比較](m2/verification/behavior.json)保留重評依據；單次五案符合沒有關閉 PNG 的波動問題。

## 已結束的模型實驗

| UTC 執行目錄 | 條件與結果 | 原始紀錄 |
| --- | --- | --- |
| `20261110T040408_395882Z` | 固定同一筆來源、medium、輸出 2,400；mini 截斷，gpt-5.6 有完整答案。 | [報告](model-comparisons/20261110T040408_395882Z/report.json)／[審閱](model-comparisons/20261110T040408_395882Z/review.md) |
| `20261110T042010_790365Z` | 兩邊實驗輸出提高為 4,800，取得完整答案供比較。 | [報告](model-comparisons/20261110T042010_790365Z/report.json)／[助理核對](model-comparisons/20261110T042010_790365Z/assistant-review.json) |

依使用者決定，現行應用使用 `gpt-5.6`，主程式輸出仍是 2,400。比較專用程式與測試已刪除；此處只保存歷史資料，不提供已移除命令作為下一步，也不從單例推論穩定優劣。

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
| 草稿動作／關閉狀態 | [draft-status.json](m2/verification/draft-status.json) |
| 動作規則後真實回歸 | [draft-status-live.json](m2/verification/draft-status-live.json) |
| 推理設定 | [reasoning-settings.json](m2/verification/reasoning-settings.json) |
| 模型實驗 2,400／4,800 | [model-comparison.json](m2/verification/model-comparison.json)／[model-comparison-4800.json](m2/verification/model-comparison-4800.json) |

這些摘要是對應開發當時的測試數與程式指紋。凍結前最新 262 項測試／Pyright 紀錄見 [M2 檢查說明](../docs/m2.md#verification)，不將早期摘要改成新結果，也不宣稱整理檔案時重新執行了測試。

## 畫面

| 畫面 | 類型與來源 |
| --- | --- |
| [M1 基本分析](m1/live-analysis.jpg) | 真實 GitHub／模型功能檢查。 |
| [相似 Issue](m2/screenshots/live-comparison.jpg)／[標籤](m2/screenshots/live-labels.jpg)／[範本](m2/screenshots/live-templates.jpg) | 真實功能檢查畫面，非品質基準。 |
| [YAML 欄位](m2/screenshots/template-yaml-offline.jpg) | 虛構表單、離線元件檢查。 |
| [草稿桌面](m2/screenshots/editable-draft-offline.jpg)／[手機](m2/screenshots/editable-draft-mobile-offline.jpg) | 虛構資料、模擬模型與互動檢查。 |

## 搬移對照與保存方式

本次將根目錄 29 份散落檔案歸入 M1／M2，其餘 82 份保留原位置；全部 111 份原證據的 SHA-256 與內容保持一致，沒有刪除失敗結果、改寫分數、重跑 API 或變更人工審閱狀態。

[manifest.json](manifest.json)列出搬移檔案的 `original_path`、目前 `path`、`sha256` 與位元組數，路徑均相對專案根目錄。歷史 JSON 的路徑、指紋表與封存 Markdown 的舊連結保持原樣；若指向舊位置，依 manifest 找新位置。舊文件路徑則查 [M2 歷史索引](../docs/m2.md#history)。目前 README、M1／M2 與 AGENTS 的引用已更新。

之後新增證據：完整執行結果維持日期目錄，手動驗證摘要與畫面放對應里程碑資料夾，再更新本索引。不要把原始 JSON 合成一份、覆寫舊報告，或把人工編輯稿當成模型原稿。
