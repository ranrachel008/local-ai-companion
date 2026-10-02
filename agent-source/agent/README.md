# Local AI Companion Agent

The product exposes **one interface: MCP over stdio**, using newline-delimited
JSON-RPC 2.0 and MCP protocol `2024-11-05`. This is not Agent-Reach and does not
install or run a candidate project. `local_model_demo.py` is a demonstration
**client of this same interface**, not another product API.

## Connect an MCP-capable local client

Use the standalone `Local-AI-Companion-Agent.exe` with its complete onedir folder,
including `_internal`. Replace every placeholder in
`client-config.example.json` with your actual paths. Your client must support
stdio MCP; its configuration format can differ from this example.

```text
Local-AI-Companion-Agent.exe --workspace C:\your-dedicated-agent-workspace --read-jobs-root C:\your-existing-jobs
```

Source equivalent (use the existing supported Python environment):

```text
python C:\your-source\scout_agent.py --workspace C:\your-dedicated-agent-workspace --read-jobs-root C:\your-existing-jobs
```

`--workspace` is owned by the Agent; keep it separate from existing GUI jobs.
`--read-jobs-root` is optional and read-only. Do not point the Agent workspace at
the original GUI jobs. Search/research/plan outputs belong to the independent
Agent workspace. Saved-job inspection must not approve, resume, install or
modify an original job.

The four discovered tools are:

| Tool | Arguments | Purpose |
| --- | --- | --- |
| `scout_search` | `goal`: string, `limit`: integer 1–10 | Search candidate projects; save Agent-owned evidence. |
| `scout_research` | `job_id`: string, `analyze`: boolean (default false) | Research the selected job; model analysis is explicitly opt-in. |
| `scout_plan` | `job_id`: string | Produce a plan without approval or execution. |
| `scout_job` | `job_id`: string | Inspect a saved job and its existing evidence. |

Unknown fields are rejected. There is no model-confirmation parameter and no
approve/execute/resume/shell tool. Installation remains a human-controlled
operation through the existing GUI; an Agent response is not installation
approval. Tools return MCP text content containing a JSON envelope with
`ok`, `status`, `data`, `error`, plus MCP `isError` for errors.

## One bounded local-model demonstration

The client uses only Python's standard library. It does not start or reconfigure
a model service, install packages, retry another provider or fall back to cloud
AI. The configured endpoint must be loopback and credential-free.

First it performs `GET /models`. It uses an actually observed model id (the
first observed id by default, or an explicitly selected observed `--model`). It
then initializes MCP, discovers tools, and sends a natural-language request with
OpenAI-compatible `tools` and `tool_choice: auto`. Only the discovered
`scout_search` is offered to the model. A real model-returned `tool_calls` value
is required; the client never invents a call from prose. After the tool result it
requests the model's final explanation. Tool turns and calls are bounded.

PowerShell example; replace paths, and run only if the existing model service is
already available:

```powershell
python 'C:\your-source\agent\local_model_demo.py' `
  --agent-exe 'C:\your-agent-folder\Local-AI-Companion-Agent.exe' `
  --workspace 'C:\your-dedicated-agent-workspace' `
  --base-url 'http://127.0.0.1:8080/v1' `
  --goal 'Search for local speech-to-text project candidates; do not research or install anything.' `
  --evidence 'C:\your-dedicated-agent-workspace\model-call-evidence.json'
```

For source launch replace `--agent-exe ...` with
`--source-script 'C:\your-source\scout_agent.py'`. Add `--model 'observed-id'`
only for an id that the endpoint actually reports. The original jobs path is
not necessary for the search-only demo.

Evidence records model discovery, selected id, request bodies, actual model
responses, model-generated tool arguments, MCP results, errors and the final
response. Common authorization/secret fields are redacted; do not include
private credentials in prompts or source data. Exit code 0 requires a successful
real tool call and final model response; otherwise it is 1. A service refusal,
unsupported model tool calling, prose-only response or tool error is a recorded
failure, not a fabricated successful Agent call. Mock tests demonstrate client
behavior only; they are not actual model or packaged-EXE acceptance. No model
compatibility is promised.

This search-only demo does not call `scout_research`; it cannot recursively
invoke research LLM analysis. Do not interpret a successful search as successful
installation or human confirmation.

## Offline client tests

```text
python -m unittest discover -s tests -p test_agent_model_client.py -v
```

These use simulated model responses and a fake stdio JSON-RPC subprocess. They
do not contact a model service, search the web or install any candidate.
