# 第一課：看懂一次模型呼叫

本課只學一個流程：準備文字，呼叫模型，取得回答。對應 Hello-Agents 第 4.1 節。

## 執行

在專案根目錄執行；這會發起一次模型請求：

```bash
uv run --locked python lessons/01_llm_call.py
```

本機 `.env` 沿用現有設定。程式沒有 CLI 選項；傳入舊選項會停止。

## 先看這一段

```python
response = client.responses.create(
    model=os.getenv("OPENAI_MODEL", "gpt-5.4-mini"),
    instructions=instructions,
    input=issue_text,
    max_output_tokens=600,
    store=False,
)
print(response.output_text)
```

- `model`：使用哪一個模型。
- `instructions`：我們希望模型遵守的規則。
- `input`：要處理的 Issue 本文。
- `response.output_text`：模型回傳的文字。

`max_output_tokens` 限制本次輸出，`store=False` 不保存成可供之後 retrieve 的 Response。這兩個參數都不等於完整的成本或資料保留政策控制。

## 為什麼先做這一步

後續 Agent 每次決策都會呼叫模型。先建立一條能看懂的最小流程，之後才能逐步辨認工具、上下文與循環各自增加了什麼能力。

優點是能直接看到輸入與回答的關係；目前不用在多個輔助函式之間跳轉。

`load_dotenv()` 載入本機設定；官方 `OpenAI` SDK 處理 API 通訊。Agent 的控制流程仍由我們自己寫。`uv run` 會使用專案的虛擬環境。

## 親手修改

只改 `issue_text`，加入或移除一項資料，再執行一次。

例如刪除 OS，檢查模型是否指出缺少 OS；提供版本後，檢查它是否還重問版本。同時確認它沒有把錯誤訊息直接當成已確認的根因。

一次結果只能說明這筆案例的表現。

## 參考

- [Hello-Agents 第 4 章](https://github.com/datawhalechina/hello-agents/tree/main/docs/chapter4)
- [OpenAI Docs：Text generation](https://developers.openai.com/api/docs/guides/text)
