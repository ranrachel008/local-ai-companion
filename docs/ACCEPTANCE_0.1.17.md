# 0.1.17 prerelease acceptance — 2026-10-02

Release gate: PASS for the scoped prerequisites. No installation approval or installation execution was performed. Public source acquisition is separate from installation approval.

| Requirement | Result | Evidence |
| --- | --- | --- |
| 1. Actual entry/schema/build | PASS | Configured stdio EXE; four tools ready; Build 20261002T095621Z-18fb7af4; EXE SHA256 e6ea5e587ff25d86517e482667f64cae1f29aab1ccb076b6db13c3c7a78acf06. |
| 2. Real daily chat chain | PASS | Sequential Qwen scout_research, scout_plan, scout_job, one call per accepted chat; successful actual tool events and final answers retained. |
| 3. Research and plan fidelity | PASS | psf/requests commit 611c6162cbc4ac2020a2f91c7cfa4f3abf9bbb60; tree SHA256 822f7ac2caff3b3e386cf009e0a43fd967ac5e2066be8f2eee2d0be91730ffe6. Author docs/user/install.rst supplies python -m pip install .; manifest supplies core dependencies. Four steps include environment, dependencies, local project installation, and human review of optional documented actions. All remain unapproved/PENDING; REVIEW_REQUIRED and UNKNOWN retained. |
| 4. Saved work after child restart | PASS | Same job queried through chat before/after Agent-only restart; all 11 saved files unchanged; source, research, plan and state retained. Source acquisition SUCCESS is not installation success. |
| 5. Necessary failure checks | PASS (simulated) | Isolated copies reject missing/invalid work, cross-work candidate and tampered tree. Injected connection, timeout and invalid response failures persist BLOCKED and prevent planning. Actual research finished in about 143 seconds under the existing 180-second limit. Real upstream outages: UNVERIFIED. |
| 6. Ordinary requirement search | PASS | Single scout_search: Python HTTP 客戶端 安裝教程, limit=3. GitHub HTTP 200; traceable requests and httpie/cli candidates. The third certbot result is less relevant. This is a search smoke test, not universal relevance validation. |
| 7. Fixes, source and package consistency | PASS | Matching exact Agent source/build metadata, complete onedir runtime and quickstart/config example; GUI runtime retained unchanged; source snapshots and asset checksums supplied. |

## Root causes and minimal fixes

Research extraction omitted plural python_environments and editable project installation evidence; planning consequently omitted the project itself. Preserve author commands and classify same-source project installation separately from dependencies. Treat equivalent README Python 3.10+ and manifest >=3.10 consistently, while retaining genuine version conflicts. Legal destination changes invalidate stale plans/approval; RUNNING or SUCCESS execution prevents destination changes. Verified source extraction safely dereferences internal archive links and rejects escapes/missing/cyclic/overlapping links. Ordinary Chinese install-tutorial wording yielded zero GitHub results; normalize client/server terms and remove tutorial wording without hardcoded repository URLs or relaxed candidate gates. File/stderr diagnostics preserve MCP stdout protocol.

## Evidence limits

The host truncates long tool results at 20,000 characters, including SSE payloads visible to Qwen. Original chat tool events and final answers are retained as received. Auxiliary read-only cached MCP queries saved complete research/plan/job returns; their prefixes and reported original lengths match the chat events, and job hashes remained unchanged. These supplements are not represented as full payloads originally shown to the model. Qwen answers honestly noted unavailable tail content; full saved state verifies the complete four-step plan.

Screenshot saving failed; existing SSE/text evidence was retained without repeating accepted tests. An earlier plan attempt exceeded the unchanged 32,768-token context (42,721); a fresh daily chat completed the plan. No parallel Qwen tasks or GUI rebuild was used. Existing model, port 8080, host startup and MCP parameters were preserved.

Compatibility tested: existing Strata with qwen3.8-flash-next-iq2_xs. Other models/hosts, complete six-language GUI coverage, independent GUI stop/cancel/failure cases and clean-machine installation: UNVERIFIED. The current round neither installs candidates nor proves installation runtime success. Author/community tutorials remain eligible with source and conditions; no separate community-tutorial case was exercised here.

GUI Build: 20261001T182710Z-030c49e0. Agent source fingerprint: 18fb7af483f8ce6a3073b16b68d72c42da15a9d9735522b4ad10013fcc2716af. GUI source fingerprint: 030c49e09443c8e003d88b62e7ce0ef0cc470df51169c969662fd5de673d4b40. Historical GUI source reconstruction/provenance is preserved in gui-source/SOURCE_PROVENANCE.md.

Public assets exclude private jobs/chat logs, credentials, models, environments and raw HTTP/SSE diagnostics. See SHA256SUMS.txt for exact asset hashes. Exact corresponding sources are in agent-source/ and gui-source/ and the separate source ZIPs.
