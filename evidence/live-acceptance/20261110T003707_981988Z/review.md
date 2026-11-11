# 必要查閱：真實回歸與草稿人工清單

這是助理來源審閱，使用者人工驗收仍為 `pending`。[原始報告](report.json)與[助理註記](assistant-review.json)分開保存。三案本輪均完成最低查閱，整體語意尚未通過驗收。

| 案例 | 實際查閱 | 執行結果 |
| --- | --- | --- |
| Flask #5804 | 搜尋 `stream_with_context teardown_request`、`stream generator teardown`，讀取並比較 #6123。 | `partial`，留言截短且最後請求無完整修正預留。 |
| Flask #4032 | 搜尋 `webtest file`、`upload_files view_args`，沒有本輪候選。 | `completed`，候選讀取／比較為 `not_needed`。 |
| Werkzeug #2948 | 搜尋 `RFC5861 cache-control`、`stale revalidate`，沒有本輪候選。 | `completed`，標籤文字矛盾修正後接受。 |

「沒有候選」僅限這些查詢。#4032 不是讀完上一輪候選，而是本輪使用的查詢沒有找到候選。程式驗證第二筆查詢不同，不驗證語意上是否真的更廣。

## 尚須人工修正

#5804 草稿仍說 `the current plan`、等待 3.2 可用，並把過去維護者對安全 workaround 的不確定寫成目前的第一人稱說法。S2 是 2025-09-13 的歷史計畫，S3 已澄清回呼本來可安全重複執行、問題在時機；這些內容不能直接推成目前建議。

#2948 草稿說正在關閉 Issue，且已解決／處理。工具沒有執行關閉，原 Issue 本來已關閉，來源也沒證明目前程式或某發布版本已實作功能。這段不可直接分享。

#4032 的最小重現補問有來源依據；仍需核對標籤、元件歸因與整體回覆是否適合這個歷史討論。

## 人工編輯參考

以下三段均是**助理另行撰寫的參考，不是原模型輸出**，沒有取代報告中的 `content`。

Flask #5804，依 [S2 歷史計畫](https://github.com/pallets/flask/issues/5804#issuecomment-3288692782)與 [S3 澄清](https://github.com/pallets/flask/issues/5804#issuecomment-3292350761)：

> Thanks for clarifying that your teardown callbacks were already idempotent; the concern was cleanup timing. In the September 2025 discussion, a maintainer expected #5812 to address the regression in Flask 3.2. Current release and implementation status have not been verified here.

Flask #4032，依 Issue 與 [維護者的最小重現要求](https://github.com/pallets/flask/issues/4032#issuecomment-840206449)：

> Your report includes version details and partial request and endpoint snippets, but not a complete minimal Flask application. The discussion requested that example, while a later comment pointed to Werkzeug; the linked issue was not retrieved here. If this is investigated again, a self-contained Flask example showing the request and expected versus actual behavior would help establish the component boundary.

Werkzeug #2948，依[原始需求](https://github.com/pallets/werkzeug/issues/2948)：

> Thanks for describing the RFC5861 directives and CDN use cases. This closed issue records a request to support `stale-while-revalidate` and `stale-if-error` in `ResponseCacheControl`. The issue state alone does not establish whether a released version implements them. Any renewed proposal should first check the current API and identify the remaining gap.

## 待使用者核對

- [ ] #5804：歷史計畫有作者與時間；沒有目前發布或安全 workaround 的未查證斷言。
- [ ] #5804：回覆承接已回答的時機澄清；候選比較有雙方本文依據。
- [ ] #4032：補問的是仍缺少的完整程式，沒有重問已提供的版本。
- [ ] #2948：沒有宣稱已執行關閉，沒有用關閉狀態證明修復。
- [ ] 三案標籤、引用與最終手動編輯都已核對。

本輪 19 次模型請求、15 次工具、98,692 個 API 回報 Token，43.16 秒。最高送出估算 11,893／12,000，無摘錄省略與結果重用。[驗證紀錄](../../m2-required-lookups-verification.json)保存 208 項離線檢查與原資料指紋。人工清單尚未勾選，M2 不宣告完成。
