# Local AI Companion Agent 0.1.17-agent.1

Download `Local-AI-Companion-Agent-0.1.17-agent.1-Windows-onedir.zip` from the [0.1.17 prerelease](https://github.com/ranrachel008/local-ai-solution-scout/releases/tag/v0.1.17-beta). Extract the entire folder and keep `Local-AI-Companion-Agent-0.1.17-agent.1.exe` beside `_internal`. Build: 20261002T095621Z-18fb7af4.
Configure your existing stdio MCP client using client-config.example.json; replace every path with your own absolute Windows paths. The example contains no model endpoint or credentials. Keep the dedicated Agent workspace separate from existing GUI jobs. The optional --read-jobs-root is read-only.

Discover tools/list before calling tools. Available tools: scout_search(goal, limit), scout_research(job_id, analyze=false), scout_plan(job_id), scout_job(job_id). Use returned job identities, never invented IDs. Research requires a selected candidate and acquired, verified source snapshot in the saved job; search alone does not prepare every research prerequisite. Human-controlled source acquisition and candidate selection use the existing Companion workflow. Public source acquisition is distinct from installation approval.

This Agent has no installation approval or execution tool. Research and plans may honestly return UNKNOWN, BLOCKED or REVIEW_REQUIRED. Installing software remains a separate human-approved GUI operation. It does not include or start a model. Use your existing MCP-capable chat host and locally configured model; compatibility was verified with Strata/Qwen only. Preserve your host's model, endpoint and launch settings.

For this acceptance, research and plans were saved without installing candidate packages, creating a candidate environment, downloading models or approving installation. Full source and build scripts are in the separately named Agent source ZIP. Historical source configuration examples use placeholder executable names: use the actual versioned EXE filename above.
