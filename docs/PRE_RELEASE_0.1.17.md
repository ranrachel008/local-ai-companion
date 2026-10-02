# Local AI Companion 0.1.17：GUI 與 MCP Agent 預發布清單

更新日期：2026-10-02。目標倉庫：ranrachel008/local-ai-solution-scout。

## 發布判斷

現有維護者提供的實機證據支持發布 Windows Beta／Prerelease。GUI 與 Agent 的用途、驗證範圍和下載包應分別說明。

這份文件是發布準備清單，尚未表示新版 Release、源碼或下載包已經上傳。建立發布 tag 前，必須把待發布包與實際驗收的版本對上。

## 已有驗證與仍待驗證項目

| 元件 | 已有證據 | 本次證據未覆蓋 |
| --- | --- | --- |
| GUI 0.1.17 | 使用者人工驗收首頁、工作紀錄、指定工作的安裝成功狀態、三個成功步驟、本體名稱及退出碼；已回報語言切換 | 完整六語，以及獨立失敗／停止／取消情境 |
| 安裝執行流程 | Agent-Reach 範例已完成隔離環境、核心依賴及本體安裝，CLI 可執行 | 其他候選及所有可選平台的安裝與配置 |
| MCP Agent | scout_search、scout_research、scout_plan、scout_job 四工具 ready；修復後日常 Qwen 聊天按指定 goal、limit=3 僅呼叫一次 scout_search，GitHub HTTP 200，返回 psf/requests 真實來源，回答引用同一來源 | scout_research、scout_plan、scout_job 的完整聊天流程；這個指定 owner/repo 測試也不能代表所有一般關鍵字搜尋都已驗證 |

已驗證的本地模型為 qwen3.8-flash-next-iq2_xs，使用既有 Strata 日常聊天宿主。勿將這份證據擴大成所有本地模型與 MCP 客戶端都已驗證。

最後聊天截圖補存失敗；已保存的完整聊天 SSE、工具返回與回答文字支持本次搜尋鏈路驗收。此限制應保留於精簡驗收記錄，不必為此重跑整條已通過的測試。

Agent 僅提供搜尋、研究、計畫及工作查詢的 MCP stdio 介面。沒有模型批准或執行安裝入口。GUI 的安裝批准與人工前置項應在說明書中分開交代。

## GitHub Releases 需要的下載檔

使用實際版本及 Build ID 命名；下表是元件清單，不是已生成的檔名。

| 檔案 | 內容 | 必要性 |
| --- | --- | --- |
| GUI Windows 程式 ZIP | 已驗收的 0.1.17 EXE 及全部隨附執行檔、內部目錄與使用說明 | 必須 |
| 最新 Agent Windows 程式 ZIP | 本次搜尋修復已生效的完整 Agent 封裝，含 EXE、全部 _internal 內容、QUICKSTART、client-config 範例及建置資訊 | 必須 |
| SHA256SUMS.txt | 本次實際發布資產的檔名及 SHA-256；以新產生的 ZIP 計算 | 必須 |
| GUI 對應源碼 ZIP | 精確對應 GUI 成品的源碼、依賴宣告及建置腳本 | 若發布 tag 尚未完整包含這份源碼，必須附上 |
| 最新 Agent 對應源碼 ZIP | 包含本次搜尋及診斷修補的精確源碼、依賴宣告及建置腳本 | 若發布 tag 尚未完整包含這份源碼，必須附上 |
| 精簡驗收記錄 | 已通過項目、未驗證項目、Build ID、來源指紋、成品雜湊與已知限制 | 放在倉庫或 Release 說明即可 |

不可只上傳單一 EXE 而漏掉它需要的內部目錄。兩個程式 ZIP 應能各自按對應說明解壓及啟動。

GitHub 自動生成的 Source code 壓縮包只反映 tag 的倉庫內容。如果 tag 裡只有 README 或舊版 ZIP，它不能代替新版應用程式源碼。

## 倉庫需要保留的檔案

- GUI 與最新 Agent 的對應源碼；沿用既有目錄結構，保留各自來源指紋及建置關係。
- 現有建置腳本與依賴宣告。
- README.md、README.zh-CN.md，以及現有其他語言說明。
- GUI 的最短使用流程及 Agent QUICKSTART。
- 從已驗證配置整理出的 client-config 範例。保持實際已驗證的 command／args 結構，改用可由使用者替換的解壓位置，勿附私人帳號路徑或憑據。
- 既有專案授權與封裝所需的第三方授權／聲明。不要自行替專案指定新的授權。
- 精簡版本更新及驗收範圍。

README 應讓普通使用者先選擇：
1. 想用視窗搜尋、研究與審閱安裝：下載 GUI。
2. 想讓現有本地聊天宿主呼叫工具：下載 Agent 並依 QUICKSTART 接入 MCP。

模型不會包含在這兩個包裡。日常使用以 GUI 或既有聊天頁操作為主。

安裝計畫說明應寫明：使用者可交由雲端 AI 協助審閱，或自己審閱後在 GUI 確認；實際批准仍由人類操作，Agent 沒有安裝執行權限。

## 發布前只做交付一致性檢查

1. 從實際正在使用的 MCP 配置定位驗收通過的新 Agent，取得 EXE 路徑、Build ID、來源指紋及雜湊。不要把修復前 agent.1 的舊 Build ID 當作新包證明。
2. 對 GUI 與 Agent 分別核對：待發布 ZIP、包內 EXE／隨附檔案、BUILD_INFO 與源碼指紋相互一致；ZIP 可以正常讀取及解壓。GUI 成品若保持原樣，不需重建。
3. 發布副本的範例配置可供其他使用者替換本機路徑；移除憑據、私人工作紀錄、聊天原文、環境目錄、模型、備份及原始 HTTP／SSE 診斷資料。保留已做脫敏的精簡驗收說明。
4. 把新源碼與文件放入發布 commit，固定 tag；若兩元件源碼不相同，分別保留精確對應的來源快照。
5. 先建立 Draft Release 並標記 Prerelease，附齊所有下載資產。核對 GitHub 上的資產清單、大小及可用雜湊／下載內容與本地一致，再發布。

既有定向測試、真實聊天搜尋及 GUI 人工驗收無需全面重跑。若發布包有新的程式碼或配置變動，只驗證受影響的部分。

## GitHub 上現有的版本

發布準備時，main 只包含 README、中文 README、.gitignore 和 Ornith-Solution-Scout-0.1.0.zip；公開 Release 為 v0.1.0-beta。請保留其歷史說明。

新版成功上傳後，更新兩份 README 的主要下載入口，清楚區分新版與舊 0.1.0 的 Ollama 固定配置限制。勿在新版資產尚未可下載時寫成「新版已發布」。
