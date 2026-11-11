# 三案來源核對與編輯後草稿

本次核對 [20261110T003707_981988Z 原始報告](report.json)保存的 Issue、留言、標籤目錄與候選本文。這是**助理逐案審閱與另行編輯**，不是新一輪模型輸出；沒有重跑模型、重新讀取 GitHub、執行範例或發布留言。來源狀態以報告的 UTC 2026-11-10 為準，未查原始碼與發布紀錄。

原報告、[先前審閱清單](review.md)與使用者人工 `pending` 狀態保持原樣。本次完成的助理審閱另記在 [editorial-review.json](editorial-review.json)。以下英文區塊可供複製，還沒有套用到既有瀏覽器任務。

## Flask #5804：保留回報者對清理時機的澄清

原草稿不可直接採用：`the current plan` 把歷史計畫當成現況；「最安全的選項」超出來源；重提回呼應能安全重複執行，也沒有承接回報者已作的澄清。

核對依據：S1 有 3.1.1／3.1.2 對照與程式；[S2](https://github.com/pallets/flask/issues/5804#issuecomment-3288692782)是 davidism 在 2025-09-13 的修復預期；[S3](https://github.com/pallets/flask/issues/5804#issuecomment-3292350761)是回報者在 2025-09-15 說明回呼本來就能安全重複執行、問題在清理時機，以及當時採用的版本固定方式。S9 的 2025-12-17 回覆仍是預期，不是發布驗證。

```text
Thanks for the detailed example and for clarifying that your teardown callbacks were already idempotent. The reported regression is the timing: in Flask 3.1.2, cleanup ran before the streaming generator began, unlike the 3.1.1 behavior shown in your report.

In September 2025, davidism expected #5812 to address this in Flask 3.2, and you reported pinning Flask below 3.1.2 at that time. The retrieved issue record is closed, but the current release status of the fix has not been verified here. That historical pin should not be treated as a verified recommendation for current deployments.
```

編輯後標籤建議只保留 `bug`，依 S1／S10 的回報內容與目錄名稱作保守分類，沒有確認根因。原 `docs` 不是已證明錯誤的標籤，但屬次要解讀；其理由談到的維護者與作者留言實際在 S2／S3，原引用只列 S1／S10，沒有支持整段理由。本次先不保留它。

候選 #6123 的 S13 本文確實已讀。S1 是提早執行 `teardown_request`；S13 是放棄生成器後留下應用程式上下文，影響後續 `teardown_appcontext`。原 `related_but_different` 比較可作**暫定的回報差異**，不代表已重現或證明根因不同。

本案仍是 `partial`：S5 留言截短，最後請求沒有完整修正預留；編輯草稿不會補回這些調查限制。原摘要的未來修復措辭同樣需要歷史歸因，這裡沒有替換原摘要。

## Flask #4032：只補尚缺少的完整重現程式

原草稿的核心補問有依據，但對已關閉的歷史報告直接提出現在式要求，應補上討論時間與條件。S1 已有 Python 3.8.5、Flask 2.0.0、請求與函式片段，無須重問版本；片段仍沒有構成完整 Flask 應用程式。

[S2](https://github.com/pallets/flask/issues/4032#issuecomment-840206449)是 davidism 在 2021-05-13 提出的最小重現要求；[S3](https://github.com/pallets/flask/issues/4032#issuecomment-841339337)是回報者後來指向 Werkzeug。所連結的跨 repo Issue 沒有被本輪工具取得，不能把歸因當成已確認。

```text
Thanks for the report. The request and endpoint snippets, together with the supplied Python 3.8.5 and Flask 2.0.0 versions, describe a WebTest upload returning HTTP 400, but they do not show a complete minimal Flask application.

In the May 2021 discussion, davidism requested that example; you later pointed to a Werkzeug issue. The linked issue was not investigated here, so the responsible component and final resolution remain unconfirmed. If this closed report is revisited, please include a self-contained application with the route setup and upload request so the reported behavior can be reproduced independently.
```

編輯後只保留 `bug` 作為回報症狀的暫定分類，依 S1／S4，不表示已確認 Flask 缺陷。原 `testing` 名稱存在於目錄，但說明空白，僅因使用 WebTest 無法確認專案如何使用它；本次保守移除，沒有判定它必然不適用。

兩次查詢沒有本輪候選，所以讀取／比較是 `not_needed`；不是證明沒有相關 Issue。原 `missing_information` 裡的「根因未知」與「最終解決方式未知」是調查限制，不能全部轉成要求作者補交的報告欄位。

## Werkzeug #2948：分清關閉狀態與功能已實作

原草稿不可直接採用：聲稱正在關閉 Issue，但工具只有讀取；又從關閉狀態推成已解決。原限制文字雖說草稿沒有猜測關閉原因，實際草稿卻作了這個推論，兩者也有矛盾。

[S1](https://github.com/pallets/werkzeug/issues/2948)有 RFC5861、兩項指令、`ResponseCacheControl` 與 CDN 用途。留言數為 0 來自 Issue metadata；留言端點沒有查閱，`comment_check.status` 仍是 `not_read`。本次只能說來源沒有提供關閉理由，不能宣稱已查完所有相關討論或實作紀錄。

```text
Thanks for explaining the request for `stale-while-revalidate` and `stale-if-error` support in `ResponseCacheControl`, including the CDN use cases.

The retrieved issue record is closed, but it does not explain the closure or establish whether those directives are supported by a released version. Before reopening this request or proposing additional API changes, the current implementation should be checked to identify any remaining gap.
```

保留 S1／S2 支持的 `standards` 建議；名稱存在且主題與規範相關，說明仍是空白，這是助理的分類判斷，不是已取得完整的專案標籤政策。沒有補問 Bug 專用欄位，也沒有要求作者重述已提供的用途。

兩次搜尋均沒有本輪候選。原 known facts 的「可用 Issue 標籤只有有限幾個」沒有說明範圍，不應推成其他目錄標籤都不能用；本次保留此原輸出問題，沒有回寫報告。

## 本輪結論與下一步

三份編輯稿已逐句對照保存的來源；原生成稿有兩份不能直接採用，另一份需要歷史語境與分類收斂。這次只完成助理審閱與改寫，沒有證明 Agent 下次會自動產生這些版本。

三案本輪都沒有讀取 Issue 範本，`templates_source_id` 與 `templates_complete` 為 `null`，所以本輪不對範本符合性作結論。先前範本功能驗證仍保存，沒有被這次審閱取代。已知 `needs-info` 波動也沒有新的驗證。

M2 的最低查閱流程已有一次真實觀察，**草稿品質尚不能直接通過驗收**。下一個小步驟建議把「宣稱未執行的維護動作」與「關閉等於已修復」加入草稿規則及針對性回歸檢查，沿用原模型、工具與預算；歷史時態盲點繼續追蹤。使用者的人工確認仍待實際完成。
