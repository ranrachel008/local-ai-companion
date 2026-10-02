# Local AI Solution Scout

[English](README.md) | [简体中文](README.zh-CN.md)

**Official website:** [Ran AI](https://ran-local-ai-scout.benzhiyanjiu.chatgpt.site)

**Find a setup others have already made work, then follow their instructions step by step.**

Deploying MiniMax H3, configuring acceleration or LoRA, or finding a practical way to run Qwen 3.8 or a 120B-class model on your own hardware can mean working through many guides. Local AI Solution Scout helps you find and read project documentation, author tutorials and community guides with working examples on GitHub, YouTube and other public platforms. It brings the sources, prerequisites, version requirements and steps together into a configuration and installation plan that AI can follow.

The desktop app can download project files and, after you approve the plan, automate supported environment setup and installation steps. It lists any configuration work that needs your attention.

## EXE, GUI and Agent

1. **EXE:** The file you double-click to start the desktop app.
2. **GUI:** The window that EXE opens, where you search, research and review installations. EXE and GUI come in the same package.
3. **Agent:** The separate version you connect to your local AI chat software so it can call search, research, plan and job tools.

## Download 0.1.17

To run the software, use one of the two downloads below. The green Code → Download ZIP menu downloads source code.

- **Use the app yourself:** [Download the desktop version (EXE / GUI)](https://github.com/ranrachel008/local-ai-solution-scout/releases/download/v0.1.17-beta/Local-AI-Companion-0.1.17-Windows-onedir.zip). Extract the whole ZIP, keep `_internal`, then double-click the EXE.
- **Let your local AI use it:** [Download the Agent version](https://github.com/ranrachel008/local-ai-solution-scout/releases/download/v0.1.17-beta/Local-AI-Companion-Agent-0.1.17-agent.1-Windows-onedir.zip), then follow the [connection guide](docs/AGENT_QUICKSTART.md). Extract the whole ZIP and keep `_internal`.

You can ask a cloud AI to help review the plan, or check it yourself; you give the final approval to execute it. The Agent provides search, research and planning tools, with no installation execution entry point. Models are supplied separately.

Windows Beta · [Release page](https://github.com/ranrachel008/local-ai-solution-scout/releases/tag/v0.1.17-beta)

<details>
<summary>Compatibility, source code and developer information</summary>

Verified with the existing Strata/Qwen setup. Other models and chat hosts remain unverified; see the [acceptance record and limits](docs/ACCEPTANCE_0.1.17.md).

[MCP configuration example](docs/client-config.example.json) · [Agent source](agent-source/) · [GUI source and provenance](gui-source/) · [Release checklist](docs/PRE_RELEASE_0.1.17.md)

Source ZIPs, the source manifest and SHA-256 checksums are available on the release page for development and verification.

</details>
