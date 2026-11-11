# M2 凍結後的新案例有限驗收

本機日期 2026-11-09（America/Los_Angeles）；執行目錄使用 UTC。M2 程式、提示、工具、檢查器、`.env` 與預算先凍結，再從三個不同 repo 選新案例。這些目標未出現在凍結前的專案案例與修改紀錄中；無法據此判斷模型預訓練是否見過公開 Issue。

[事先保存的驗收協議](protocol.json)、[案例標準](cases.json)與[獨立 GitHub 選取紀錄](selection.json)均在模型執行前固定。[原始結果](report.json)保存真實 API 輸出，[助理來源核對](assistant-review.json)另外記錄判斷。使用者人工審閱維持 `pending`。

## 本輪決定

**這輪有限功能驗收通過，可進入 M2 收尾；程式繼續保持凍結。** 三案均有接受的最終答案，沒有依預先標準判定的阻塞性問題。這項結論適用本輪範圍，不是生產準確率或所有分支都已實測的保證。

| 新案例 | 結果 | 主要核對 |
| --- | --- | --- |
| [HTTPX #3602](https://github.com/encode/httpx/issues/3602) | `completed`；6 次模型／5 次工具 | 正確區分預發布版本存在與 API 移除是否故意；未假稱 `Transport` 可直接替代。推薦 `release`，兩次成功空搜尋的範圍有說明。 |
| [pytest #14560](https://github.com/pytest-dev/pytest/issues/14560) | `partial`；5 次模型／4 次工具 | 保留作者與維護者的不同說法，提出最小重現需求並承接 explicit ID 建議。推薦 `topic: parametrize`，沒有重複既有的 needs-information 標籤。 |
| [Ruff #29177](https://github.com/astral-sh/ruff/issues/29177) | `completed`；6 次模型／5 次工具 | 辨識 lint 規則需求，讀取 #25014 本文後提出暫定 `possible_duplicate`，推薦 `rule`；沒有假稱已讀文件或查證發布版本。 |

使用 `gpt-5.6`／`medium`，每案只跑一次。每案仍限 8 次模型請求、15 次工具、90 秒、12,000 本機輸入估算、2,400 輸出 Token、20 候選及 5 筆候選本文。總計 **17 次模型請求、14 次工具、81,782 個回報 Token、72.92 秒**。17 個已送輸入全部符合預算；最大估算為 9,634，含實際預留的最大合計為 12,000。沒有逾時、API 錯誤、輸出耗盡或無最終答案。

## 為什麼接受 pytest 的部分完成

事先的協議已允許「有最終答案、誠實揭露範圍」的有界部分結果。pytest 讀了 11 則中的前 10 則，省略已取得的 S5／S11，五筆候選都未讀。準備讀候選時未達最低 2,400 預留，程式改用現有證據收尾。

它沒有聲稱讀完全部留言、沒有相關 Issue 或已完成重複比對；兩份省略摘錄仍保留在 Sources。原始自動檢查 `minimum_similar_issue_checks_completed=false` 完整保留，沒有改成通過。這是覆蓋限制，依原協議不單獨阻擋本輪交付。

## 保留的限制

- HTTPX 的 `missing_information` 多是 API 意圖、遷移路徑與後續版本等調查未知，放在限制區會更清楚；沒有因此推薦 `need-info`。
- Ruff 草稿以「appears to be covered」推論既有規則可能涵蓋，但最後建議直接使用該規則的語氣偏強；應先核對實際行為與版本。其限制已明示文件、程式及發布內容未查證。摘要的 PEP 810 名稱可在 S5 找到，但摘要本身只引用 S1／S2，細部引用仍可更精確。
- 本輪未查範本或 README，也沒有答案被拒、進入修正的事件。修正狀態仍由凍結前的離線回歸支持，不能說這輪已現場驗證修正。
- 完整模型重播位元組未存入原報告，這次不宣稱已獨立核對所有重播內容。三筆有目的選取的歷史案例也不足以估計一般使用的準確率。

以上依事先標準記為非阻塞觀察或未演練項目；本輪沒有因結果改碼、改提示、改評分條件或重新付費挑答案。

## 凍結與可重現性

166 個既有檔案與 `.env` 均核對未變。三個目標的實際本文也與選取時內容相同。案例、選取證據與協議的 SHA-256 分別為：

- 案例：`5d3946523219d059b87ce421f63445dffc7ed092a9fbf3e11270da74460163e2`
- 選取：`ae70754609ffc7c0496e918d344b18a44046908681e62159bab372df19f99126`
- 協議：`bd84dc70073dee9ae3ee004a720b9f4545c9f9c4207097be9b35d0148048a0fd`
- 原始結果：`ab6051135015cb1d456ae3b2c419c787a30947206975a69c83c8b47b9dff25ca`

沿用既有 `scripts.acceptance.main()`，只在執行邊界選擇新的案例與選取檔案；原本三案與 runner 程式未覆寫。驗收條件與選取資料沒有進入 AgentRun，模型只收到原流程的目標 URL、設定與真實工具結果。

本次執行命令如下；它會使用真實模型額度。本輪已完成，不需要為了取得同樣結果再次執行。

```bash
uv run --locked python -c 'import asyncio; from pathlib import Path; import scripts.acceptance as acceptance; acceptance.CASES_PATH=Path("evaluation/m2_frozen_acceptance_cases.json").resolve(); acceptance.SELECTION_PATH=Path("evaluation/m2_frozen_acceptance_selection.json").resolve(); asyncio.run(acceptance.main())'
```

凍結前 262 項離線測試與 Pyright 零錯誤的紀錄沿用；這輪沒有改程式，沒有重新宣稱執行過這些檢查。

練習：比較 pytest 的 `partial`、原自動覆蓋檢查的 `false`，以及本輪協議對有界部分交付的規定，說明為什麼「執行狀態」、「查閱覆蓋」與「交付是否符合這次標準」需要分開記錄。

接下來保留此版作為下一里程碑的基準，不再針對這批案例細修。這三筆已成為看過的驗收案例，之後若依它們調整程式，就不能再稱為全新的驗證資料。

## 原始草稿

下列文字直接來自本輪接受的模型輸出，沒有人工修飾。

<details>
<summary>httpx_prerelease_transport</summary>

Thanks for documenting the upgrade path and exact exception. This showed that moving from 0.28.1 to 1.0.dev1 removed the top-level `httpx.BaseTransport` export. On July 14, 2025, lovelydinosaur confirmed that the 1.0 pre-release was intentionally available on PyPI and said more information would follow, but the discussion captured here did not clarify whether this particular API removal was intentional or whether `Transport` was a compatible replacement. Anyone revisiting this should consult the current 1.0 API and migration documentation rather than assuming the suggested `Transport` name is a drop-in replacement; the intended replacement and migration path should be documented explicitly.

</details>

<details>
<summary>pytest_custom_object_ids</summary>

Thanks for the additional explanation. The failure occurred while pytest was deriving an automatic ID for the parameter value: it queried `__name__`, and the instance’s custom attribute lookup raised `KeyError`. As discussed above, missing attributes should not escape from `__getattr__` as `KeyError`; the instance can instead raise `AttributeError` for an unknown attribute.

For an object that intentionally keeps that behavior, the supported workaround is to provide the test ID explicitly rather than modify pytest internals:

```python
pytest.param(MyBroken(), id="broken")
```

The report did not include a minimal reproducer or the exact pytest versions before and after the reported change, so it was not possible from this discussion to establish a pytest regression. If this is revisited, please include the smallest custom class and parametrized test that demonstrate it, along with the last working and first failing pytest versions.

</details>

<details>
<summary>ruff_lazy_import_rule</summary>

Thanks for the concise example. This case appears to be covered by the existing `lazy-import-immediately-resolved` rule linked above. The earlier proposal in #25014 explicitly included lazy-imported names used at unconditional module scope, which matches the assignment shown here. The recommendation is therefore to use that existing rule rather than add a separate diagnostic.

</details>
