# Local AI Solution Scout

[English](README.md) | [简体中文](README.zh-CN.md)

找到别人已经跑通的软件方案，整理安装方法，再审阅安装计划。

## EXE、GUI、Agent 是什么？

1. **EXE：** 双击它，就能启动窗口版软件。
2. **GUI：** EXE 打开后的操作窗口，用来搜索、研究和审阅安装。EXE 和 GUI 在同一个下载包里。
3. **Agent：** 给本地 AI 调用的版本，接入聊天软件后，让 AI 帮你搜索、研究和生成计划。

## 下载哪一个？

- **自己用窗口操作：** [下载窗口版（EXE / GUI）](https://github.com/ranrachel008/local-ai-solution-scout/releases/download/v0.1.17-beta/Local-AI-Companion-0.1.17-Windows-onedir.zip)。完整解压，保留 `_internal`，然后双击里面的 EXE。
- **让本地 AI 使用：** [下载 Agent 版](https://github.com/ranrachel008/local-ai-solution-scout/releases/download/v0.1.17-beta/Local-AI-Companion-Agent-0.1.17-agent.1-Windows-onedir.zip)，按[接入说明](docs/AGENT_QUICKSTART.md)连接已有聊天软件。完整解压并保留 `_internal`。

Agent 负责研究和生成计划；安装需要另行由人类审阅确认。模型需另行准备。

Windows Beta 测试版 · [发布页](https://github.com/ranrachel008/local-ai-solution-scout/releases/tag/v0.1.17-beta)

<details>
<summary>兼容性、源码与开发者资料</summary>

已在既有 Strata/Qwen 环境验证。其他模型和聊天软件尚未验证，详见[验收记录与限制](docs/ACCEPTANCE_0.1.17.md)。

[MCP 配置示例](docs/client-config.example.json) · [Agent 源码](agent-source/) · [GUI 源码与来源说明](gui-source/) · [发布清单](docs/PRE_RELEASE_0.1.17.md)

发布页中的源码包、源码指纹和 SHA-256 清单供开发与核对使用。

</details>

<details>
<summary>历史 0.1.0-beta 说明</summary>

> **兼容性更正 — v0.1.0-beta（2026-09-30）**
>
> 已发布的 Windows 安装包目前**不能自由选择本地 AI 模型或后端**。其中的 AI 功能固定连接 `http://127.0.0.1:11434` 的 Ollama，并要求模型标签为 `ornith-prefilter:35b`。仅安装 Qwen 或其他本地模型并不能直接使用。此前关于已发布 Beta「不绑定某一个 AI 模型」的说明不准确，对此我们道歉。
>
> 此说明仅适用于历史 v0.1.0-beta 包；新版请使用上方独立的 0.1.17 预发布下载入口。

**找到已经跑通的方案，照着做，剩下的交给本地 AI。**

Local AI Solution Scout 是一个面向普通用户的本地 AI 软件工具。

它的核心思路很简单：

**不要让 AI 从零猜怎么安装软件，而是先找到别人已经成功跑通的方案，再根据可靠来源复现。**

## 它能做什么

- 搜索已经成功运行的软件方案
- 查找 GitHub、教程、官方文档等公开来源
- 找到原始软件和官方下载来源
- 整理安装步骤
- 使用本地 AI 帮助安装和排错
- 减少阅读大量复杂教程的时间

简单来说：

**先找作业，再抄作业。**

## 本地 AI — 历史发布版的限制

对已发布的 `Ornith-Solution-Scout-0.1.0.zip` 进行静态检查后发现：

- `product_config.py` 固定使用 `ollama`、地址 `http://127.0.0.1:11434` 和模型名 `ornith-prefilter:35b`。
- `local_only_test.py` 在推理前通过 `/api/tags` 检查这个确切的模型标签，再向 `/api/generate` 发送请求。
- 在已检查的程序代码中，未找到供用户切换模型、后端或覆盖上述配置的入口。
- 普通名称的 Qwen 不会被自动选中；目前的连接实现也不能直接连接 OpenAI 兼容服务。
- AI 查询规划失败时，搜索有降级路径，因此部分搜索功能可能仍可使用。但缺少指定服务或模型时，AI 查询扩展和安装资料的 AI 分析会受影响。能打开软件、能搜到结果，不代表 AI 连接已正常。

以上是安装包静态检查结果，尚未完成干净 Windows 环境的完整运行验证。满足固定服务和模型条件，也不等于所有流程均已验证可靠。

自由选择本地模型和后端需要修改程序、重新构建并测试发布；不能将其当作这个 Beta 已经具备的能力。

## 当前状态

**公开 Beta 测试版**

目前正在使用真实的软件安装任务进行测试。

## 下载

历史 [v0.1.0-beta 程序包](https://github.com/ranrachel008/local-ai-solution-scout/releases/tag/v0.1.0-beta) 存在上述限制，这些限制仅适用于该历史包。新版 GUI 与 MCP Agent 已提供独立的 [0.1.17 预发布下载](https://github.com/ranrachel008/local-ai-solution-scout/releases/tag/v0.1.17-beta)，使用范围与已验证兼容性见上方说明。

## 支持平台

目前主要针对：

- Windows 10
- Windows 11

## 核心理念

**不要重复造轮子。找到已经跑通的方案，然后复制成功的方法。**

</details>
