# IssueLens

以 GitHub Issue 調查為題，從零學習 Agent Engineering。Agent 循環、工具、上下文管理與評估自行實作，使用一般 OpenAI Python SDK，不依賴 Agent 框架。

目前是本機 Next.js＋FastAPI 應用程式：貼上公開 Issue URL，由單一 Agent 讀取證據、比較候選、推薦標籤並產生可編輯的英文草稿。程式碼與 Prompt 使用英文，教學說明使用繁體中文。

## 文件入口

每個里程碑各維護一份文件，功能與最新狀態直接更新在對應文件，不再逐次新增說明檔。

| 文件 | 內容 |
| --- | --- |
| [M1：本機應用程式](docs/m1.md) | 環境、啟動／停止、架構、任務 API、來源與基本操作。 |
| [M2：Issue 調查與草稿](docs/m2.md) | 七個工具、模型與預算、Context Builder、答案修正、草稿編輯、評估、最新驗收與已知限制。 |
| [M3：可靠性與持久化](docs/m3.md) | PostgreSQL 分析紀錄、獨立 Worker、執行檢查點、有界重試與 Trace。 |
| [M4：公開試用](docs/m4.md) | GitHub 登入、分析存取權限與雙帳號驗收。 |
| [M5：在 Claude Code 與 Codex 中使用](docs/m5.md) | 本機 MCP server、`analyze_github_issue` 工具、設定與協定版本。 |

原始報告、驗證摘要與畫面由 [evidence 證據索引](evidence/README.md)分類整理；功能開發歷程見 [M2 歷史索引](docs/m2.md#history)，舊輸出保持原樣。

## 快速啟動

需要 Python 3.13 以上、uv、Node.js 20.9 以上與 npm。在專案根目錄執行：

```bash
uv sync --locked
uv run --locked python scripts/dev.py
```

第一次使用，先依 [M1 環境設定](docs/m1.md#環境與啟動)建立 `.env` 並填入 `OPENAI_API_KEY`；已有設定就保留原檔。目前使用 `gpt-5.6`／`medium`，`GITHUB_TOKEN` 可選，未設定時匿名讀取公開資料。Key 只由後端讀取。

- 網頁：<http://127.0.0.1:3000>
- API 文件：<http://127.0.0.1:8000/docs>
- 啟動器會啟動前端和 API。設定 PostgreSQL 與模型 Key 時，也會啟動背景 Worker。`Ctrl+C` 停止啟動器管理的程序。
- 按下 `Analyze issue` 建立由 Worker 執行的分析，過程會呼叫真實 GitHub 與模型 API。

本機已設定 PostgreSQL。API 重啟期間 Worker 仍可執行。Worker 重啟後也可在原預算內從檢查點接續，原網址可讀取進度和結果。`DATABASE_URL` 留白時使用記憶體模式。草稿編輯保存在前端記憶體，重新整理會回到模型原稿。[M3 啟動與重啟](docs/m3.md#本機設定與啟動)

## 目前進度

M1 基本流程已完成。M2 功能保持凍結，最新三個不同 repo 的案例均有最終答案，依事先標準通過本輪有限功能驗收；其中一案部分完成，使用者人工審閱仍待確認。

最新來源核對、未演練分支與保留限制見 [M2 驗收](docs/m2.md#acceptance)。這不是生產準確率，也不代表所有語意問題都已修復。

M3 已完成 PostgreSQL 分析紀錄、獨立 Worker、執行檢查點、有界重試與 Trace。替換 Worker 後可沿用已保存的回合，並用原任務 ID 查詢模型與工具嘗試。[查詢 Trace](docs/m3.md#trace) · [M3 驗證](docs/m3.md#檢查與本次驗證)

M4 已接入可設定的 GitHub 登入，登入後建立的分析由帳號持有。[OAuth 設定與驗收](docs/m4.md)

M5 第一步讓 Claude Code 與 Codex 透過本機 MCP server 呼叫 IssueLens 分析 issue。兩個客戶端已完成連線並取得工具清單；Codex 已實際分析一個公開 issue，約 20 秒完成並回報進度，Claude Code 尚未實際分析。[設定與驗證](docs/m5.md)

## 保留的入門課程

這兩課是小型線性範例，與產品的自主循環分開。下列命令都會呼叫真實模型 API，沒有 CLI 選項。

| 課程 | 命令 | 筆記 |
| --- | --- | --- |
| 一次模型呼叫 | `uv run --locked python lessons/01_llm_call.py` | [Lesson 01](lessons/01_llm_call.md) |
| 一次工具往返 | `uv run --locked python lessons/02_tool_call.py` | [Lesson 02](lessons/02_tool_call.md) |

Lesson 02 使用固定虛構 Issue，第一輪指定工具、第二輪回答，正常共兩次模型請求。切換資料直接修改程式中的 `issue_number`；舊 `--dry-run`／`--issue` 會在發送請求前停止。

## 離線檢查

```bash
uv run --locked python -W error::RuntimeWarning -m unittest discover -s tests -q
```

離線測試使用模擬的 GitHub 與模型回應。未設定測試資料庫時會跳過 15 項 PostgreSQL 檢查。[M3 資料庫整合檢查](docs/m3.md#檢查與本次驗證)／[M4 登入驗收](docs/m4.md#兩個帳號驗收)／[M2 模型評估](docs/m2.md#verification)
