# 三筆真實 Issue 驗收紀錄

執行日期：2026-11-09。模型：`gpt-5.4-mini`。模型與 GitHub API 均為真實呼叫；GitHub 為匿名讀取。每案執行一次，未調整 Prompt、模型或預算重跑。

**以下判讀由助理撰寫，使用者人工驗收仍是待完成。M2 尚未宣告通過收尾。**

| 案例 | 類型 | 程式執行狀態 | 助理初看 |
| --- | --- | --- | --- |
| [Flask #5804](https://github.com/pallets/flask/issues/5804) | 完整 Bug Report | completed | 需要修正：漏讀討論、標籤用途不符。 |
| [Flask #4032](https://github.com/pallets/flask/issues/4032) | 資訊不足回報 | partial | 需要修正：留言狀態錯誤、補問失焦、輸出矛盾。 |
| [Werkzeug #2948](https://github.com/pallets/werkzeug/issues/2948) | Feature Request | completed | 大致合理，歷史討論的草稿仍待確認。 |

三案皆產生程式接受的最終 JSON。共 20 次模型請求、17 次工具呼叫、81,173 個回報 Token，45.59 秒。執行狀態與內容品質分開判讀，沒有換算成三案通過率。

## 1. 完整 Bug：Flask #5804

問題是 Flask 3.1.2 的串流回應在產生內容前就呼叫 teardown，與 3.1.1 的順序不同。原文提供可執行程式、版本及 traceback。[目標 Issue](https://github.com/pallets/flask/issues/5804)

本次完成 6 次模型請求、5 次工具呼叫，保存 5 個來源；最大輸入估算 9,396 Token。模型讀取目標、標籤、搜尋結果、候選 #6123 與 README，沒有讀留言。

摘要大致符合原文。`related_but_different` 的比較引用 S1 與 S4，指出候選是串流放棄與跨執行緒回收造成 app context 留存，和目標的 teardown 時機不同。搜尋另有 #5774，但未讀取其本文，不能視為已比較。[候選 #6123](https://github.com/pallets/flask/issues/6123)

主要待修正事項：

- 目標資料已知有 8 則留言。獨立核對的維護者留言包含歷史 3.2 處理方向與 teardown 指引，模型卻先讀 README，再在草稿建議繼續 bisect 或確認是否修好。模型有誠實揭露沒讀留言，但調查與草稿仍漏掉重要背景。[維護者留言](https://github.com/pallets/flask/issues/5804#issuecomment-3288692782)
- S2 中 `python` 的說明為 `Pull requests that update Python code`，模型卻以涉及 Python 實作為由推薦給 Issue。名稱及引用合法，理由仍未符合標籤用途。`bug` 建議則有具體回歸回報支持。

這些問題發生時仍有剩餘預算；沒有 Context Builder 內容省略。本次沒有讀取 PR 實作或驗證任何版本已修好。

### 模型原稿，未修改

> Thanks for the detailed reproduction and version comparison. This does look like a regression in the 3.1.2 `stream_with_context()` teardown ordering: the teardown callback is being invoked before the response generator starts, which breaks `g` access during streaming and then triggers a second teardown on exit.
>
> The next step is to bisect or review the 3.1.2 `stream_with_context()`/request context changes that could have moved teardown execution earlier in the lifecycle. If you can confirm whether this is already fixed in a later release or on `main`, that would help narrow the regression window.

人工審閱：

- [ ] 已核對 S1、S2、S4 與獨立留言。
- [ ] 已確認對漏讀留言與標籤用途的判讀，或記錄不同意見。
- [ ] 已決定草稿需要哪些修改。

使用者結論／備註：待填。

## 2. 資訊不足：Flask #4032

回報指出 Flask 2.0.0 下，透過 webtest 上傳檔案會得到 400；提供 Python 3.8.5、部分用戶端呼叫與 endpoint 函式，但沒有完整 Flask 應用程式與 route 設定。[目標 Issue](https://github.com/pallets/flask/issues/4032)

本次完成 7 次模型請求、6 次工具呼叫，保存 7 個來源；最大輸入估算 6,855 Token。兩次搜尋都沒有保留候選；模型讀到 Bug／Feature Markdown 範本，沒有讀留言。

主要待修正事項：

- 目標中已知有 2 則留言，摘要卻寫成沒有留言可用。這是把「沒有讀取」當成「沒有資料」。維護者實際曾要求最小 Flask 重現範例，後續回報者另連結其他 Repository 的 Issue；後者只是歸因主張，沒有在本次模型調查中驗證。[維護者留言](https://github.com/pallets/flask/issues/4032#issuecomment-840206449)、[回報者後續留言](https://github.com/pallets/flask/issues/4032#issuecomment-841339337)
- 補問要求根因、更多版本界線及 traceback，沒有優先詢問缺少的完整 Flask 重現程式。根因尚未確認，不等於回報者必須先知道根因。
- `limitations` 說沒有新增標籤建議，實際清單卻有 `bug` 和 `testing`，前後矛盾。
- 模型還嘗試以 `get_related_issue` 再讀目標 #4032。後端在發出候選請求前擋下，保留警告並標為 `partial`。這是工具規劃錯誤，但範圍保護確實生效。

### 模型原稿，未修改

> Thanks for the report and the reproducer. The issue description is clear: the `file` parameter is missing from `req.view_args` in Flask 2.0.0, which leads to a 400 for this `webtest` upload case.
>
> Could you share the full traceback or any additional server-side error output from the 400 response? That would help confirm whether this is a Flask routing/request parsing regression or an interaction with the test client setup.

人工審閱：

- [ ] 已核對留言數、工具事件與摘要中的留言說法。
- [ ] 已判斷第一個補問是否應改為完整 Flask 重現程式。
- [ ] 已確認標籤清單與限制文字矛盾。

使用者結論／備註：待填。

## 3. 功能需求：Werkzeug #2948

回報要求在 `ResponseCacheControl` 支援 `stale-while-revalidate` 與 `stale-if-error`，並描述 CDN 合併請求及故障時使用過期內容的情境。[目標 Issue](https://github.com/pallets/werkzeug/issues/2948)

本次完成 7 次模型請求、6 次工具呼叫，保存 8 個來源；最大輸入估算 7,330 Token。模型讀取目標、標籤、兩份範本、兩次搜尋與 README。

本次助理初看大致合理：模型辨認為功能請求，使用 Feature Request 範本，沒有要求 Bug 才需要的環境資訊；Markdown 欄位的必填狀態維持未知。`standards` 與 RFC 擴充目的相符，但該標籤沒有說明，不能額外發明 Repository 政策。

兩次搜尋都沒有保留候選，因此本案沒有比較內容可驗收。Issue 已關閉且留言數為 0；模型沒有宣稱某個版本已實作完成，但草稿仍像新的功能討論，是否適合該歷史討論串，留待維護者判斷。

### 模型原稿，未修改

> Thanks for the detailed request and the RFC reference. Supporting `stale-while-revalidate` and `stale-if-error` in `ResponseCacheControl` would be a standards-aligned feature addition, and the use cases you described are clear. Since this is a feature request rather than a bug, could you also outline any expected API shape or compatibility constraints you’d want preserved in Werkzeug’s cache-control objects?

人工審閱：

- [ ] 已核對功能名稱、用途、標籤與 Feature Request 範本。
- [ ] 已確認草稿沒有把關閉狀態當成修復證明。
- [ ] 已判斷這個補問對已關閉的歷史討論是否合適。

使用者結論／備註：待填。

## 下一個小步驟

建議先補強「有重要留言時的調查選擇」及「未讀／不存在／讀取失敗」的區分，再分別處理 PR 用途標籤與輸出矛盾。保留本輪為原始基準，修改後另外建立新報告。

原 PNG 案的 `needs-info` 波動仍開放追蹤。本輪兩個標籤目錄都沒有 `needs-info`，不能用沒有推薦該標籤來認定問題已修好。

[原始報告](report.json) · [機器可讀的助理註記](assistant-review.json) · [模型執行前的獨立來源](selection.json) · [方法與操作](../../../docs/m2_live_acceptance.md)
