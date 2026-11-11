# 草稿證據使用：後續回歸與人工清單

本紀錄為助理來源審閱；使用者人工驗收仍為 `pending`。[首輪失敗](../20261109T234613_939464Z/review.md)完整保留。本輪[原始報告](report.json)與[助理註記](assistant-review.json)另存。

| 案例 | 結果 | 助理觀察，尚非人工驗收 |
| --- | --- | --- |
| Flask #5804 | `partial`，有最終答案 | 修復計畫已歸因給維護者，也不再直接重問冪等性；「等 3.2 可用」仍暗示未查證的目前狀態。相似搜尋未執行，一則留言截短。 |
| Flask #4032 | `partial`，有最終答案 | 補問缺少的最小 Flask 程式，沒有重問版本；曾比較未讀候選，驗證擋下後修正，仍有候選未比較。 |
| Werkzeug #2948 | `completed` | 不再斷言目前缺少該功能，詢問 API 形狀而非重複 CDN 用途；是否適合回覆這個歷史已關閉討論，仍待判斷。 |

#5804 的來源：

- [S2：2025-09-13 維護者計畫](https://github.com/pallets/flask/issues/5804#issuecomment-3288692782)是歷史說法，沒有驗證目前發布版本。
- [S3：回報者澄清](https://github.com/pallets/flask/issues/5804#issuecomment-3292350761)指出其實際回呼已可安全重複執行，問題在執行時機。

以下是**助理人工撰寫的參考，不是模型原稿**：

> Thanks for clarifying that your teardown callbacks were already idempotent; the concern was cleanup timing. In the September 2025 discussion, a maintainer expected #5812 to address the regression in Flask 3.2. Current release and implementation status have not been verified here.

這個參考可用於人工編輯練習，不會取代 `result.content` 或變更本輪評分。

## 待使用者核對

- [ ] #5804：把歷史計畫與未查證的現在狀態分開；刪除沒有依據的「等版本可用」安排。
- [ ] #5804：確認草稿承接 S3 的時機澄清，且沒有要求重做已回答的處理。
- [ ] #4032：確認完整最小程式仍缺少，並檢查標籤是否適合。
- [ ] #2948：確認 API 形狀補問有用，且沒有把歷史請求當成目前能力缺口。
- [ ] 核對搜尋未執行、候選未比較與留言截短，決定是否需要補調查。

## 執行與限制

本輪 19 次模型請求、13 次工具、95,932 個 API 回報 Token，54.72 秒。最高送出估算 9,672，加預留後最高 9,806／12,000。兩案實際使用了保留修正輪數的流程；三案都有最終答案，但不能稱為 M2 全部通過。

#5804 第 4 輪因標籤矛盾被拒、第 5 輪因未歸因修復預告被拒，第 6 輪接受；相似搜尋仍未完成。#4032 第 6 輪的未讀候選比較被拒，第 7 輪修正。#2948 首次最終答案通過。拒絕原因、候選文字與輸出指紋保存在原報告。

本步共兩次真實回歸；首輪沒有最終答案的案例不會被後續結果覆蓋。語意仍有盲點，[最後驗證紀錄](../../m2-draft-grounding-final-verification.json)明確保留這些限制與 187 項離線檢查。人工清單尚未勾選，M2 不宣告收尾。
