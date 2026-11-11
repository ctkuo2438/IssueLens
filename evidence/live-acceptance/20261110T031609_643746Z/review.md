# 三案真實回歸：M2 尚不能收尾

本機日期 2026-11-09，時區 America/Los_Angeles；原報告使用 UTC 2026-11-10。這輪使用真實 `gpt-5.4-mini` 與匿名 GitHub，同三案各執行一次，模型、程式與預算保持不變。[原始報告](report.json)完整保留；[助理來源審閱](assistant-review.json)另存，使用者人工審閱仍為 `pending`。

## 執行結果

| 案例 | 執行狀態 | 模型／工具 | 來源核對結論 |
| --- | --- | --- | --- |
| Flask #5804 | `no_final_answer` | 6／6 | 取得 #6123 後，下一次輸入超過本機預算，沒有比較或草稿可驗收。 |
| Flask #4032 | `completed` | 6／5 | 最小重現補問有依據；元件歸因、歷史語境與 `testing` 理由仍需保守處理。 |
| Werkzeug #2948 | `completed` | 6／5 | 草稿沒有假稱關閉或已修復；限制文字卻與 `standards` 建議矛盾。 |

共 18 次模型請求、16 次邏輯工具呼叫、89,225 個 API 回報 Token，31.06 秒。這些是執行用量，不是模型正確率；有兩份可接受 JSON，不等於兩案語意通過。

## #5804：沒有把候選本文送到模型

目標 S1、8 則留言 S2–S9、標籤 S10 與兩次搜尋 S11／S12 已取得。第二次查詢 `stream teardown` 發現 #6123、#5774、#2188、#3566；只有 #6123 本文 S13 被讀取，另三筆未讀。

第 7 次請求的 Context Builder 最後估算為 **12,049／12,000**，已含原本的 1,024 Token 估算餘裕。保留 256 Token 修正空間與取消預留的兩次建構都放不下，因此第 7 次模型請求沒有送出。這是同一個待送請求的兩次本機建構，不是額外兩次 API 呼叫。

S13 保存在 Sources，但該建構沒有送出，模型未看到這段候選本文；不能說已完成模型比較，更不能把沒有草稿當成正確棄答。沒有發生摘錄省略或相同結果引用，已暴露的歷史保持原樣。S5 另有原本的 350 Token 截短限制。

和前一輪比較，S1–S11 與 S13 摘錄相同，第二次查詢及候選集合則不同；不能只把差異歸因於提示增加的 21 Token，或直接判定是模型能力不足。本輪的直接停止原因是程式的輸入預算條件。

## #4032：核心補問正確，歸因仍需更清楚

S1 已提供 Python 3.8.5、Flask 2.0.0、請求與端點片段；[S2](https://github.com/pallets/flask/issues/4032#issuecomment-840206449)要求完整最小 Flask 範例。草稿沒有重問版本，核心補問有依據。

[S3](https://github.com/pallets/flask/issues/4032#issuecomment-841339337)是回報者猜測 Werkzeug 的說法，跨 repo 的連結沒有被本輪讀取。草稿說目前證據顯示不是 WebTest 單獨的問題，仍應清楚歸因給回報者，而不是讓助理的推論代替查證。對這個已關閉的 2021 年討論，改成「若重新調查」的條件式補問也更清楚。

原模型草稿，未編輯：

```text
Thanks for the report and the follow-up. The current evidence suggests this is not specific to WebTest alone, but we still need a minimal Flask-only reproduction to verify where the regression is happening. Could you provide a small example that uses Flask directly and shows `file` disappearing from `view_args`, or confirm whether the linked Werkzeug issue fully explains the behavior?
```

兩次查詢沒有本輪候選，不表示 repo 沒有相關 Issue。S4 目錄確有 `bug`／`testing`，但說明空白；`bug` 可作回報症狀的暫定分類，`testing` 僅因使用 WebTest 而推薦仍偏弱，並非已證明不適用。原 missing information 中的歷史歸因與影響範圍未知，也不應全部當成作者必填欄位。本案沒有讀範本，不對範本符合性作結論。

## #2948：新草稿避開動作錯誤，仍有標籤矛盾

S1 支持 RFC5861、兩項指令、`ResponseCacheControl` 與 CDN 用途；S2 包含 `standards`。S5–S7 取得目前 default branch 的 Markdown Bug／Feature 範本，必填狀態仍未知。沒有把功能需求要求成 Bug 重現，也沒有從關閉推定已實作。

原模型草稿，未編輯：

```text
Thanks for the feature request. The report clearly identifies the RFC5861 extensions `stale-while-revalidate` and `stale-if-error`, along with the cache/CDN use cases they would support. Since this is a feature request rather than a bug report, the main follow-up would be API design: what should the `ResponseCacheControl` interface look like for these directives, and should the values be parsed/serialized as first-class attributes or handled generically?
```

這段沒有先前的第一人稱關閉及已解決宣稱；但尚未確認目前實作，API 設計問題應以仍有功能缺口為條件，也應承接已關閉的歷史狀態。

直接矛盾在 `content.limitations[1]`：它說 `no label recommendation is made`，`label_recommendations` 卻列出 `standards`。原報告的自動 `label_decision_consistent=true` 保持不變；這是有限英文檢查的漏判，助理語意審閱判為未通過，沒有用自動通過掩蓋它。

留言數 0 來自 metadata；留言端點仍是 `not_read`，不是查閱後確認為空。本輪沒有任何答案修正事件，因此不能宣稱新動作／狀態檢查已在真實 API 中成功觸發修正；該流程仍以既有離線模擬為證。

## 驗收決定與模型問題

**M2 目前不能收尾。** 一案缺少最終答案，另一案有明確內容矛盾；這兩點已足以不通過本輪驗收。使用者人工狀態維持待確認，不因助理審閱完成而自動勾選。

較強模型值得測試。依 [GPT-5.4 Mini 官方說明](https://developers.openai.com/api/docs/models/gpt-5.4-mini)，mini 的原生上下文上限是 400,000 Token；這次的 12,000 是程式自訂政策。換模型可能改變規劃和文字品質，但不能保證解除這個本機停止條件。

使用者提出模型因素後，下一步建議先以**相同保存來源、相同提示與界限**比較 mini 與 [GPT-5.4](https://developers.openai.com/api/docs/models/gpt-5.4) 的最終輸出，觀察歸因、矛盾與成本，再決定預設模型。這只隔離資料固定時的輸出品質，不代表完整工具規劃或搜尋能力已驗證。這輪尚未切換設定，也沒有額外模型對照請求。

程式方面另需處理：在新留言首次暴露前預留必要候選輸入空間，避免直到候選抵達才發現固定歷史已塞滿；標籤文字檢查也漏了本次的單數現在式。這兩項獨立記錄，不靠提高模型等級當成已修復。

[驗證、指紋與用量](../../m2-draft-status-live-verification.json)另存；88 個既有檔案、原案例與全部舊報告保持不變。程式未修改，因此本輪沒有重跑上一步的 229 項離線測試或型別檢查。
