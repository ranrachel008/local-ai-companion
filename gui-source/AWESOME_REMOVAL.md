# Local AI Companion 0.1.11 — Awesome 移除與本機重建

範圍：僅移除 Awesome Selfhosted 補充搜尋，產品聚焦本地 AI 方案。
原交付 Build ID：20260930T174251Z-62bd7ea8。
本源碼已於 Windows 使用既有 Python 3.10.11／PyInstaller 環境正式重建。
新 Build ID：20260930T183342Z-1fd769c2。

## 已移除

- awesome_catalog.py 及 normal_ui.py 中的搜尋接入、故障分支、專用安裝閘門、詳情標籤和 candidate-only 處理。
- data/awesome-selfhosted-data：1,351 個 YAML 及 CATALOG_COMMIT。
- build/verify-0.1.11-awesome.py。
- 0.1.11 spec 中的 Awesome 必需目錄與打包資源。

保留通用 provenance 合併，既有 YouTube／網站、AI／Web、GitHub、Hugging Face 搜尋及安裝流程。
只移除此來源，未另外加入全域非 AI 結果過濾功能。
產品版本維持 0.1.11，BUILD_INFO 已由正式建置腳本重建；根模組指紋為
`1fd769c245a2dfd67f5f5d2ce6dae5d57cac1020f62095695e5bbe36d9755d20`。

## 驗證

- 所有 Python 源碼語法編譯：PASS。
- NormalProductApp UI AST 與原 source ZIP 相同：PASS。
- 除 normal_ui.py 外，其餘 26 個根目錄 Python 模組與原 ZIP 逐位元相同：PASS。
- 無 awesome_catalog、awesome-selfhosted-data、awesome_candidate_only 執行／打包引用：PASS。
- Awesome 模組及資料目錄已移除：PASS。
- BUILD_INFO 所有模組 SHA256：PASS。
- python build/verify-0.1.3.py：12/12 PASS。
- python build/verify-0.1.10-search.py：修改前後均為 14/18；相同 2 FAIL + 2 ERROR。
  既有問題：跨段落性能證據、related README timeout 分級、卡片 stargazers_count、卡片 model_id；測試未修改。
- Windows EXE 建置及封裝內容核對：PASS。
- GUI、即時網路端到端及實際安裝：UNVERIFIED。

## Windows 本機重建

在本包解壓出的源碼根目錄，用既有 Python／PyInstaller 環境執行這一整行：

```powershell
& '.\build\build-release.ps1' -Python 'C:\Users\ASUS\AppData\Local\Programs\Python\Python310\python.exe'
```

輸出：build/dist/Local-AI-Companion-0.1.11/Local-AI-Companion-0.1.11.exe。
原 ZIP 保留作為回退版本；不要覆蓋正在執行的舊版 EXE 或使用者資料。
