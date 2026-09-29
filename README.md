# Local AI Solution Scout

> **Compatibility notice — v0.1.0-beta (2026-09-30)**
>
> The published Windows package does **not** currently let users freely select a local AI model or backend. Its AI features are fixed to Ollama at `http://127.0.0.1:11434` and the model tag `ornith-prefilter:35b`. Having Qwen or another local model installed is not sufficient. The earlier claim that the released Beta was not tied to one model was inaccurate; we apologize.
>
> A corrected application package has **not** been published. This documentation correction does not fix the existing EXE. Please read the limitations below before downloading.

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

## Local AI — current release limitations

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

The existing v0.1.0-beta package is available from the Releases page above, with the limitations described here. No corrected package is available yet.

## Platform

Currently focused on:

- Windows 10
- Windows 11

## Philosophy

**Don't reinvent the setup. Find a working one and copy what works.**
