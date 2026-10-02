# Local AI Solution Scout

[English](README.md) | [简体中文](README.zh-CN.md)

**Find a setup others have already made work, then follow their instructions step by step.**

Deploying MiniMax H3, configuring acceleration or LoRA, or finding a practical way to run Qwen 3.8 or a 120B-class model on your own hardware can mean working through many guides. Local AI Solution Scout helps you find and read project documentation, author tutorials and community guides with working examples on GitHub, YouTube and other public platforms. It brings the sources, prerequisites, version requirements and steps together into a configuration and installation plan that AI can follow.

The desktop app can download project files and, after you approve the plan, automate supported environment setup and installation steps. It lists any configuration work that needs your attention.

## EXE, GUI and Agent

1. **EXE:** The file you double-click to start the desktop app.
2. **GUI:** The window that EXE opens, where you search, research and review installations. EXE and GUI come in the same package.
3. **Agent:** The separate version you connect to your local AI chat software so it can call search, research, plan and job tools.

## Download

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

<details>
<summary>Historical 0.1.0-beta documentation</summary>

> **Compatibility notice — v0.1.0-beta (2026-09-30)**
>
> The published Windows package does **not** currently let users freely select a local AI model or backend. Its AI features are fixed to Ollama at `http://127.0.0.1:11434` and the model tag `ornith-prefilter:35b`. Having Qwen or another local model installed is not sufficient. The earlier claim that the released Beta was not tied to one model was inaccurate; we apologize.
>
> This notice applies only to the historical v0.1.0-beta package; use the separate 0.1.17 prerelease links above.

**Official website / 官方網站：** [Ran AI](https://ran-local-ai-scout.benzhiyanjiu.chatgpt.site)

**Find what already works. Copy the setup. Let local AI do the rest.**

## Existing Windows Beta — read limitations before downloading

[Open the Releases page](https://github.com/ranrachel008/local-ai-solution-scout/releases/tag/v0.1.0-beta)

Local AI Solution Scout helps beginners find proven software setups and reproduce them on their own computer.

Instead of asking AI to invent an installation method from scratch, it looks for existing working solutions, checks the sources, and helps local AI follow the proven setup.

## What it does

- Find existing working software setups
- Search GitHub, tutorials, documentation, and other public sources
- Identify the original software source
- Build an installation plan
- Use local AI to help install and troubleshoot software
- Designed for users who do not want to spend hours reading technical tutorials

## Local AI — historical release limitations

Static inspection of the published `Ornith-Solution-Scout-0.1.0.zip` found:

- `product_config.py` fixes the provider to `ollama`, the address to `http://127.0.0.1:11434`, and the model to `ornith-prefilter:35b`.
- `local_only_test.py` checks `/api/tags` for that exact model tag before sending inference requests to `/api/generate`.
- No user-facing model/backend selection or configuration override was found in the inspected application code.
- An ordinary Qwen model tag is not automatically selected. OpenAI-compatible services are not directly supported by this connection implementation.
- Search has a fallback when AI query planning fails, so some search functions may remain usable. AI query expansion and AI-assisted installation research are affected when the required service/model is absent. Opening the app or obtaining search results does not validate the AI connection.

This was a static package audit, not a clean Windows end-to-end runtime test. Matching the fixed service/model settings alone does not establish full workflow reliability.

Supporting selectable local models/backends requires an application change and a newly built, tested release. It is not a capability verified in this Beta.

## Status

**Public Beta**

The project is being tested with real software installation tasks.

## Download

The existing v0.1.0-beta package is available from the Releases page above, with the limitations described here. This historical package is separate from 0.1.17 above.

## Platform

Currently focused on:

- Windows 10
- Windows 11

## Philosophy

**Don't reinvent the setup. Find a working one and copy what works.**

</details>
