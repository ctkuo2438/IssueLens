# 固定來源比較：2,400 Token 對哪個模型足夠

本機日期 2026-11-09，時區 America/Los_Angeles。使用 Flask #5804 的同一份保存資料，各送一次真實請求；effort 都是 `medium`、輸出上限都是 2,400。沒有重新查 GitHub、重試、答案修正或中途提高額度。

**這筆資料下，GPT-5.6 交出完整答案，mini 因輸出額度耗盡而截斷。** 一案一次不能證明一般品質或穩定性；mini 沒有完整草稿，所以不能把這輪當成兩份完整答案的品質排名。

## 實際用量

| 請求模型 | API 輸入 | 全部輸出 | 其中推理 | 其餘輸出* | 狀態 | 秒數 |
| --- | ---: | ---: | ---: | ---: | --- | ---: |
| `gpt-5.4-mini` | 9,165 | 2,400 | 2,201 | 199 | `incomplete`：`max_output_tokens` | 17.62 |
| `gpt-5.6` | 9,165 | 1,585 | 516 | 1,069 | `completed`，JSON 符合 schema | 17.43 |

\*「其餘輸出」是 API 輸出扣掉推理，可能包含格式成本，不等同正文的精確計費 Token。本機另估計可見文字為 190／1,059 Token。單次耗時不能推估平均速度。

Mini 幾乎把額度用在推理，最後停在 known facts 的文字中，尚未輸出草稿、比較與標籤。完整原文保存在 [report.json](report.json)，不修補片段或把它當成正確棄答。

GPT-5.6 在這次回應剩下 815 Token 空間。這只能說這一筆、這個提示及 effort 下足夠，不能保證另外兩案或下一次都足夠。API 實際回報模型分別為 `gpt-5.4-mini-2026-03-17` 與 `gpt-5.6-sol`。

簡化的標準文字費用估算為 USD 0.01767375／0.06836，**不含快取寫入費及其他附加費**。GPT-5.6 回報 9,162 個 cache-write tokens，因此不能把第二個數字當成最終帳單。價格依 [Mini](https://developers.openai.com/api/docs/models/gpt-5.4-mini)／[GPT-5.6 Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol) 官方頁面，查核日期為本機 2026-11-09；實際 usage 保留於原始報告。

## 條件是否一致

[protocol.json](protocol.json)在兩次請求前保存選材、順序與審閱條件。兩邊共用同一個 [common-request.json](common-request.json)，只改 `model`；共用請求指紋為 `2b29cc068e7e9899cd89a4935cd2437390275c0ba03c5be508c41c5a3e756dd4`。

輸入來自 [031609 舊報告](../../live-acceptance/20261110T031609_643746Z/report.json)的 13 個來源摘錄與查閱 metadata，不含舊答案、評分、執行結果或人工參考。S5 仍保留原本的 350 Token 截短限制。原 run 未送出的 S13 候選本文，這次與其他來源一起直接供兩個模型閱讀。

本機輸入估算為 11,197／12,000，含 1,024 估算餘裕；API 兩邊實際輸入均為 9,165。這是把保存資料交給模型作一次最終綜合，不是重跑自主工具流程，也不是修復原先凍結歷史塞滿的問題。

基礎提示沿用應用程式規則，另外明確說明資料已提供、無工具可用；實際使用的全文保存於 [instructions.txt](instructions.txt)。工作期間偵測到 `agent.py` 有其他變更，沒有覆蓋它；兩份模型輸入仍使用同一個封存提示。本輪沒有舊推理項目，不把不同模型的多輪推理保留預設混進對照。

## GPT-5.6 的來源核對

本輪實際草稿，未編輯：

```text
Thanks for the detailed reproduction and follow-up. The report showed that Flask 3.1.2 invoked `teardown_request` before iterating the `stream_with_context` generator and then invoked it again, while affected users worked around the regression by pinning Flask below 3.1.2. The maintainer discussion expected #5812 to address this in 3.2 and also noted that teardown callbacks should remain safe across multiple invocations. Since the collected discussion does not include verification against a released 3.2 build, the concrete recommendation is to rerun the original minimal example on the applicable 3.2 release and report the exact version and output if the premature teardown remains observable.
```

- 歷史修復與版本固定：S2／S9 是維護者過去對 3.2 的預期，S3 是作者當時固定版本的說法。答案使用歷史歸因，並明確保留目前發布／修復未知；草稿仍可補明確日期，且把「適用的 3.2 發布版本」寫成以實際存在且適用為條件的建議。
- 清理時機與安全重複執行：S1／S3 支持主要問題在提早清理。known facts 正確寫出回呼本來就能安全重複執行；草稿重述維護者警語時，仍可直接承接作者已作的澄清。
- 相似 Issue：只比較已提供本文的 #6123，引用 S1／S13。提早／重複執行 `teardown_request` 與放棄生成器後留下應用程式上下文，是來源中有描述的不同條件；沒有因其他候選未讀而製造比較，沒有證明根因不同。
- 標籤：只推薦 `bug`，名稱存在於 S10 目錄，引用 S1／S10，沒有「零推薦」與清單矛盾。目錄說明空白，主題相關性仍是審閱判斷。
- 動作與驗證：沒有第一人稱重現、假稱關閉或由關閉推成已修復。未知範本查閱與截短留言限制有揭露。

上述是助理逐案核對，使用者人工狀態仍為 `pending`。[assistant-review.json](assistant-review.json)依預先列出的五項條件另存判讀；格式與已知句型通過不等於完整 Agent 最終驗證或語意保證。原模型內容未改寫。

## 結論與下一步

這次證據支持「GPT-5.6 在這筆資料、同額度下較能完成任務」，不能證明它在所有 Issue 都更準，也不能宣布 M2 完成。Mini 的完整品質因截斷而無法評估。

若要繼續做**完整答案品質**比較，下一步建議只把實驗的兩邊輸出額度一起改成 4,800，保留相同來源、提示與 `medium`，再各送一次。這是新的受控實驗，不保證一定足夠；本輪 2,400 的結果及主程式設定保持原樣，尚未執行下一輪。

[驗證指紋](../../m2-model-comparison-verification.json)保存 95 個原檔案與來源／共用請求核對；248 項離線測試及 Pyright 通過，其中 9 項新增測試。離線測試均使用模擬回覆，與本輪兩次真實請求分開記錄。
