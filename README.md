# Local AI Companion

Find proven local AI tools, verify official sources, and turn installation instructions into clear, reviewable plans.

本地 AI 伴侣帮助你寻找已跑通的本地 AI 软件与工作流程，核对官方来源，并将安装说明整理成清晰、可审查的计划。

## Download / 下载

### Windows Desktop / Windows 桌面版

[⬇ Download Local AI Companion for Windows](https://github.com/ranrachel008/local-ai-companion/releases/download/v0.1.23/Local-AI-Companion-0.1.23-Windows-x64.zip)

下载 ZIP 后，选择“全部解压缩”，再双击 `Local-AI-Companion-0.1.23.exe`。

Download the ZIP, extract all files, then double-click `Local-AI-Companion-0.1.22.exe`.

### Agent / MCP 代理程式

[⬇ Download Local AI Companion Agent](https://github.com/ranrachel008/local-ai-companion/releases/latest/download/Local-AI-Companion-Agent-0.1.22-agent.1-flat-fixed.zip)

Agent 供 MCP 或兼容的 AI 客户端使用，不是普通桌面用户必需的组件。一般 Windows 用户请下载上面的桌面版。Agent 需要 Microsoft Visual C++ x64 Redistributable。

The Agent package is for MCP and compatible AI clients. Most Windows users only need the desktop application. The Agent requires the Microsoft Visual C++ x64 Redistributable.

> 请使用上面的正式下载链接。GitHub 绿色的 **Code → Download ZIP** 下载的是仓库源代码，不是可直接使用的软件包。
>
> Use the official links above. GitHub’s green **Code → Download ZIP** downloads repository source code, not the application package.

## What it does / 它能做什么

- Finds mature local AI software, tools, and workflows  
  寻找成熟的本地 AI 软件、工具与工作流程
- Prioritizes official repositories, documentation, and original publications  
  优先核对官方仓库、说明文档与原始发布内容
- Extracts documented installation requirements, commands, launch steps, and verification details  
  提取文档中的安装条件、命令、启动步骤与验证方式
- Produces a structured plan that you can inspect before taking action  
  生成可在执行前逐项检查的结构化计划
- Marks unknown, incomplete, or risky information instead of pretending certainty  
  对缺失、不完整或风险未知的信息明确标记，不假装确定

## How to use / 使用方式

1. Start the desktop application.  
   启动桌面程序。
2. Search for the local AI tool or workflow you want.  
   搜索你想使用的本地 AI 工具或工作流程。
3. Open a result and review its source, repository, installation method, prerequisites, commands, and launch instructions.  
   打开结果，审查来源、仓库、安装方法、前置条件、命令与启动说明。
4. Review the generated plan before approving any supported installation step.  
   在确认任何受支持的安装步骤前，先审查生成的计划。
5. Complete any manual steps that the software identifies as requiring your confirmation.  
   对软件标记为需要人工确认的步骤，由你自行决定是否继续。

## Local AI assistance / 本地 AI 协助

Using a local model is optional. When connected, Local AI Companion can help extract and organize installation information from verified source text.

本地模型是可选的。连接后，本地 AI 可以协助从已验证的来源文字中提取并整理安装信息。

To avoid endless retries, local-AI analysis has a fixed call budget. It stops early when official instructions are found, and stops for review when a source is incomplete, untrusted, truncated, or unclear.

为避免无限循环，本地 AI 分析有固定调用上限。找到官方步骤会提前结束；来源不完整、不可信、输出截断或信息不清楚时，会停止并要求你审查。

## Safety boundary / 安全边界

Local AI Companion is a research-and-planning tool. It does not treat an installation plan as proof that third-party software is safe or suitable for your computer.

本地 AI 伴侣是研究与规划工具。安装计划不代表第三方软件已经被证明安全，也不代表它一定适合你的电脑。

Supported installation actions require your approval. When the application cannot verify an instruction or source, it should stop and show what needs your confirmation.

受支持的安装操作需要你的确认。程序无法验证某条指令或来源时，应停止并显示需要你确认的内容。

## Agent / MCP usage

The optional Agent package exposes research and planning tools to compatible MCP clients. Installation-related tools are disabled by default and must be explicitly enabled by the user.

可选的 Agent 包向兼容 MCP 客户端提供研究与规划工具。安装相关工具默认关闭，只有用户明确启用后才会开放。

See the bundled `README.md`, `launch-agent.ps1`, and `compliance/MCP-SETUP.md` after extracting the Agent package.

解压 Agent 包后，请阅读其中的 `README.md`、`launch-agent.ps1` 与 `compliance/MCP-SETUP.md`。

## Verify downloads / 校验下载文件

Download [SHA256SUMS.txt](https://github.com/ranrachel008/local-ai-companion/releases/latest/download/SHA256SUMS.txt) and compare its values with the ZIP files you downloaded.

下载 [SHA256SUMS.txt](https://github.com/ranrachel008/local-ai-companion/releases/latest/download/SHA256SUMS.txt)，将其中的值与下载到的 ZIP 文件进行比对。

## Limitations / 限制

- Source pages and video descriptions can be incomplete or change over time.  
  来源页面与视频说明可能不完整，也可能随时间变化。
- Some software requires accounts, licences, GPU drivers, manual configuration, or unsupported steps.  
  部分软件需要账户、许可证、GPU 驱动、手动配置或不受支持的步骤。
- You remain responsible for reviewing commands, passwords, network access, licences, and data-handling implications before installation.  
  安装前，请自行审查命令、密码、网络访问、许可证与数据处理影响。
