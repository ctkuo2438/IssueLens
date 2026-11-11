# 第二課：看懂一次工具往返

本課只看三段流程：模型請求工具、Python 執行工具、模型取得結果後回答。工具名稱與說明的設計可對照 Hello-Agents 第 4 章。

## 執行

在專案根目錄執行，正常流程會發起兩次模型請求：

```bash
uv run --locked python lessons/02_tool_call.py
```

程式沒有 CLI 選項。切換資料時，直接修改 `issue_number = 123`，例如改成 `124`。

## 1. 先看真正的工具

```python
def get_issue(issue_number):
    path = Path(__file__).parent / "fixtures" / "issues.json"
    issues = json.loads(path.read_text(encoding="utf-8"))
    return issues[str(issue_number)]
```

它只是一般 Python 函式，依編號讀取固定 JSON 檔案。資料都是虛構案例。

為什麼先用本地資料？可以先把網路、認證與 GitHub 配額的影響排除，專注看工具流程；之後再替換成真實 API。

## 2. 看模型收到的規格

`tool` 描述工具名稱、用途及參數格式。它讓模型知道可以請求什麼；Python 函式則負責真正執行。

`parameters` 要求一個整數 `issue_number`；`strict` 要求參數符合 Schema。Schema 只限制格式，程式仍檢查工具名稱與目標編號。

## 3. 看第一次模型請求

第一次只送出「讀取某個 Issue」的要求與工具規格，尚未送入 Issue 本文。

```python
tool_choice={"type": "function", "name": "get_issue"}
```

這一課固定指定工具，讓你先理解協定。模型會產生工具參數；之後才讓它自主決定使用哪個工具。

回應可能包含其他項目，所以程式從 `first.output` 找出 `function_call`，不假設第一個項目一定是工具呼叫。

## 4. 看 Python 如何執行

```python
arguments = json.loads(call.arguments)
result = get_issue(arguments["issue_number"])
```

模型回傳名稱與參數，這兩行才是本地讀取發生的位置。`arguments` 是 JSON 字串，先解析成 Python 字典，才能取得編號。

模型提出請求，執行權仍由程式掌握。

## 5. 看結果怎麼交回去

```python
history.extend(cast(ResponseInputParam, [item.to_dict() for item in first.output]))
history.append({
    "type": "function_call_output",
    "call_id": call.call_id,
    "output": json.dumps(result),
})
```

`call_id` 由 OpenAI API 回應提供。交回結果時沿用同一個 ID，才能配對到原本的工具請求。

保留完整的 `first.output`，也能保留模型可能需要續接的狀態。這裡的 `history` 只存在本次執行中，尚未持久化。

接著第二次模型請求收到這份歷史與工具結果，才產生分析。

## 為什麼需要這幾個型別標註

`tool: FunctionToolParam` 告訴型別檢查器，這個字典是 SDK 規定的函式工具格式。

`history: ResponseInputParam` 表示列表裡可以放 SDK 支援的輸入項目，包括使用者訊息、工具請求和工具結果。未標註時，檢查器會依第一筆資料推斷成一般的字串字典列表，無法正確理解後續加入的項目。

`first.output` 裡面是 SDK 回應物件。`to_dict()` 實際把它們轉成字典，供下一次請求使用；這也保留了可能需要續接的加密狀態。

序列化後的字典會失去精確的靜態結構資訊，因此在這個邊界用 `cast(ResponseInputParam, ...)` 標明用途。`cast` 只提供型別資訊，不會轉換資料或執行驗證；實際轉換發生在 `to_dict()`。

這些標註能讓編輯器檢查 API 輸入格式，沒有增加 Agent 框架或新的執行步驟。

## 為什麼採用直線寫法

第一次請求、工具執行、第二次請求都直接出現在同一個檔案，不用跳到 Runner 或 Dispatcher 才能追蹤流程。

目前只有一個工具，因此先直接呼叫對應函式。工具變多時，再練習 Tool Registry 與參數驗證；需要多輪調查時，再建立有終止條件的 ReAct Loop。

這個版本保留基本的完成狀態、工具名稱及目標編號檢查。更完整的驗證、重試與錯誤分類會另外教學。

## 核對結果

先用 #123，檢查工具回傳與最後回答是否一致。接著把 `issue_number` 改成 124：

- 工具請求與結果的編號都應是 124。
- #124 沒有版本與 OS，回答不能沿用 #123 的資訊。
- 回答應遵守整理證據與詢問缺漏資訊的規則。

## 面試取捨

這是固定兩輪的工具流程，尚未完成自主 Agent。選擇先拆開工具規格、實際執行與結果回傳，是為了清楚理解控制權和資料流，再逐步加入自主決策。

## 離線程式檢查

```bash
uv run --locked python -m unittest discover -s tests -v
```

測試使用模擬回應，檢查兩次請求的資料流與工具結果配對；不讀 API Key、不連線，也不衡量模型品質。

## 參考

- [Hello-Agents 第 4 章](https://github.com/datawhalechina/hello-agents/tree/main/docs/chapter4)
- [OpenAI Docs：Function calling](https://developers.openai.com/api/docs/guides/function-calling)
