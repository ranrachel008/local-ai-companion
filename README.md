# Local AI Companion 0.1.22

## Download / 下載

### Windows Desktop / Windows 桌面版

[**⬇ Download Local AI Companion 0.1.22 for Windows**](https://github.com/ranrachel008/local-ai-companion/releases/latest/download/Local-AI-Companion-0.1.22-Windows-x64.zip)

下載 ZIP 並完整解壓，然後雙擊 `Local-AI-Companion-0.1.22.exe`。

Download the ZIP, extract all files, then double-click `Local-AI-Companion-0.1.22.exe`.

### Agent / 代理程式

[**⬇ Download Local AI Companion Agent 0.1.22**](https://github.com/ranrachel008/local-ai-companion/releases/latest/download/Local-AI-Companion-Agent-0.1.22-agent.1-Windows-x64.zip)

Agent 適用於 MCP 與相容的 AI 客戶端；普通 Windows 用戶請下載上面的桌面版。需要先安裝 Microsoft Visual C++ x64 Redistributable。

The Agent package is intended for MCP and compatible AI clients. Most Windows users should download the desktop app above. Microsoft Visual C++ x64 Redistributable is required.

> 請使用上面的正式下載連結。GitHub 綠色的 **Code → Download ZIP** 只會下載倉庫原始碼，不是軟件安裝包。
>
> Use the official download links above. GitHub's green **Code → Download ZIP** downloads repository source files, not the application package.

## What's new in 0.1.22 / 本版更新

- 本地 AI 分析有明確輪數與時間上限；找不到可靠安裝步驟、來源不可信或輸出截斷時會停止並要求確認，不會無限循環。
- 進度會顯示目前分析的來源與已使用模型輪數。
- GitHub 與 Hugging Face 優先讀取原始 Markdown，並保留文件中的原樣命令。
- 支援驗證後的第一方 YouTube 頻道貼文安裝方法；其他來源仍需使用可驗證的程式碼區塊命令。
- 改善 Node / npm 專案與頻道貼文安裝說明的提取。
- Local AI 回應失敗或設定不正確時，會回退至確定性流程，不會中斷搜尋。
- Agent 發行包現在只包含執行所需檔案，不再混入原始碼與測試。

- Local-AI analysis now has explicit call and time limits. It stops for review instead of looping when sources are untrusted, steps are missing, or output is truncated.
- Progress shows the source currently being analysed and the model-call budget used.
- GitHub and Hugging Face documentation now prefers original Markdown and preserves documented commands verbatim.
- Verified first-party YouTube channel posts can provide installation methods; other sources still require verifiable command evidence.
- Improved extraction for Node / npm projects and channel-post installation instructions.
- Invalid Local-AI settings or model failures fall back to the deterministic path rather than stopping research.
- The Agent release package now contains runtime files only, without bundled source code or tests.

## About / 產品介紹

**Local AI Companion** helps you find proven local AI tools and workflows, verify original sources, and turn installation instructions into a clear, reviewable plan.

**本地 AI 伴侶**幫助你尋找已經跑通的本地 AI 工具與工作流程，核對官方原始來源，並把安裝說明整理成清晰、可審查的安裝計畫。

## Features / 主要功能

- Find proven local AI tools and workflows  
  尋找成熟、已驗證的本地 AI 工具與工作流程
- Verify official and original sources  
  核對官方與原始發佈來源
- Generate reviewable installation plans  
  生成可逐步檢查的安裝計畫
- Stop safely and request confirmation when evidence is insufficient  
  資訊不足、來源不可信或有風險時安全停止並要求確認
- Optional MCP Agent with explicit installation opt-in  
  可選 MCP Agent；安裝能力必須明確啟用與確認

## Release files / 發行文件

- [Windows Desktop / Windows 桌面版](https://github.com/ranrachel008/local-ai-companion/releases/latest/download/Local-AI-Companion-0.1.22-Windows-x64.zip)
- [Agent / 代理程式](https://github.com/ranrachel008/local-ai-companion/releases/latest/download/Local-AI-Companion-Agent-0.1.22-agent.1-Windows-x64.zip)
- [SHA-256 checksums / SHA-256 校驗](https://github.com/ranrachel008/local-ai-companion/releases/latest/download/SHA256SUMS.txt)
- [Open the latest release / 查看最新發行版](https://github.com/ranrachel008/local-ai-companion/releases/latest)

Windows Desktop ZIP SHA-256: `dc733ce264c5bf57b72ea755b4dd5e46daab94e9ba0a6f67f9c27e002e1d39ca`

Agent ZIP SHA-256: `9b15622f03296102ca3e1afcf8af55d761bdc7afede96ef49fc0eade24770230`
