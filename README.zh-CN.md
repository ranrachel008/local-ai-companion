# Local AI Solution Scout

[English](README.md) | [简体中文](README.zh-CN.md)

**官方网站：** [Ran AI](https://ran-local-ai-scout.benzhiyanjiu.chatgpt.site)

**先找前人跑通的方案，再照着说明书一步步配置。**

部署 MiniMax H3、配置加速和 LoRA，或为 Qwen 3.8、120B 级别的大模型寻找适合自己硬件的运行方法，常常要翻很多说明书和教程。Local AI Solution Scout 帮你查找、阅读并整理 GitHub、YouTube 等平台上的项目说明、作者教程和社区实操经验，把来源、关键前提、版本要求和操作步骤整理成配置与安装计划，让 AI 按照前人已经跑通的方法去做。

窗口版可自动下载项目文件；你确认计划后，它会自动执行已支持的环境配置和安装步骤。需要你处理的配置会明确列出来。

## EXE、GUI、Agent 是什么？

1. **EXE：** 双击它，就能启动窗口版软件。
2. **GUI：** EXE 打开后的操作窗口，用来搜索、研究和审阅安装。EXE 和 GUI 在同一个下载包里。
3. **Agent：** 给本地 AI 调用的版本，接入聊天软件后，让 AI 帮你搜索、研究和生成计划。

## 下载 0.1.17

运行软件请用下面两个下载入口；绿色 Code → Download ZIP 下载的是源码。

- **自己用窗口操作：** [下载窗口版（EXE / GUI）](https://github.com/ranrachel008/local-ai-solution-scout/releases/download/v0.1.17-beta/Local-AI-Companion-0.1.17-Windows-onedir.zip)。完整解压，保留 `_internal`，然后双击里面的 EXE。
- **让本地 AI 使用：** [下载 Agent 版](https://github.com/ranrachel008/local-ai-solution-scout/releases/download/v0.1.17-beta/Local-AI-Companion-Agent-0.1.17-agent.1-Windows-onedir.zip)，按[接入说明](docs/AGENT_QUICKSTART.md)连接已有聊天软件。完整解压并保留 `_internal`。

安装计划可请云端 AI 协助审阅，也可以自己检查，最终由你确认执行。Agent 版负责搜索、研究和生成计划，不提供安装执行入口。模型需另行准备。

Windows Beta 测试版 · [发布页](https://github.com/ranrachel008/local-ai-solution-scout/releases/tag/v0.1.17-beta)

<details>
<summary>兼容性、源码与开发者资料</summary>

已在既有 Strata/Qwen 环境验证。其他模型和聊天软件尚未验证，详见[验收记录与限制](docs/ACCEPTANCE_0.1.17.md)。

[MCP 配置示例](docs/client-config.example.json) · [Agent 源码](agent-source/) · [GUI 源码与来源说明](gui-source/) · [发布清单](docs/PRE_RELEASE_0.1.17.md)

发布页中的源码包、源码指纹和 SHA-256 清单供开发与核对使用。

</details>
