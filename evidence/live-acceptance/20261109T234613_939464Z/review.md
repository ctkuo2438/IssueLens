# 草稿證據使用：第一次回歸，未通過

這是助理來源審閱；使用者人工驗收仍為 `pending`。[原始報告](report.json)與[機器註記](assistant-review.json)保留本輪失敗。這輪之後另補修正輪數保留政策，結果將另外保存。

| 案例 | 本輪結果 | 草稿觀察 |
| --- | --- | --- |
| Flask #5804 | `no_final_answer` | 第 7 輪的未歸因修復預告被攔下，第 8 輪又出現標籤文字矛盾，沒有有效最終草稿。 |
| Flask #4032 | `partial` | 補問仍聚焦缺少的完整 Flask 重現，不重問已提供的 Python／Flask 版本。搜尋有候選未比較。 |
| Werkzeug #2948 | `completed` | 草稿沒有宣稱目前仍缺該功能；對 API 形狀與是否重開的詢問仍待使用者評估。 |

#5804 的 S2 是 2025-09-13 維護者提出的歷史修復計畫，S3 是回報者說明回呼已具冪等性、問題在執行時機。第 7 輪仍把計畫寫成目前預告，並給出泛用重複執行建議；第 8 輪雖使用過去式，歸因、回補修復認知及草稿用途仍有疑慮。兩份遭拒草稿不可視為接受的答案。

以下只供人工對照，**由助理另行撰寫，不是本輪模型接受的輸出**：

> Thanks for clarifying that your teardown callbacks were already idempotent; the reported concern was when cleanup ran. In September 2025, a maintainer expected #5812 to address the regression in Flask 3.2. The current release and implementation status have not been verified here.

這個例子對應 [S2：歷史計畫](https://github.com/pallets/flask/issues/5804#issuecomment-3288692782)與 [S3：已回答的澄清](https://github.com/pallets/flask/issues/5804#issuecomment-3292350761)，沒有要求重做已說明的處理，也沒有宣稱某個發布版本已修復。

本輪共 21 次模型請求、17 次工具、115,510 個 API 回報 Token，49.4 秒。最高送出估算 11,911／12,000；沒有摘錄省略，一次相同目錄結果重用。第 7 輪完整預留不合而改用零預留最後嘗試，第 8 輪修正仍可送出；最終失敗是答案驗證與輪數耗盡，不是超額請求被發出。

[初次驗證紀錄](../../m2-draft-grounding-verification.json)保存 185 項離線測試與當時程式指紋。這份結果不因後續調整而改寫；M2 尚未通過，使用者人工清單維持待審閱。
