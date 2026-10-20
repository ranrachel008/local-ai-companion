# AGENTS.md — Local AI Companion

> **繁體中文摘要**：Local AI Companion 是一個 Python 3.10.11 + Tk 的 Windows 桌面程式（用 PyInstaller 6.22.3 打包成 onedir，含 `_internal`）。它透過 OpenAI 相容介面連接本地 AI，先搜尋 @ranai-korea YouTube 頻道（權重最高、使用者已驗證的來源），再搜 GitHub 和 Hugging Face，研究安裝方法並產生安裝計劃。計劃停在 `READY_FOR_APPROVAL` / `REVIEW_REQUIRED`，未經批准絕不安裝。修改時：自己頻道的貼文永遠排第一；開啟本地 AI 不能比關閉更差；不自動執行安裝命令；保留來源驗證；保留 CRLF/LF；六種語言同步；在新副本裡工作，未經擁有者同意不發佈。修 bug 先寫會失敗的回歸測試，再修，再審查。

## What this app does

- Windows desktop app, **CPython 3.10.11 + Tkinter** (standard library only at runtime). Packaged with
  **PyInstaller 6.22.3** (+ `packaging` 26.3, see `requirements-build.txt`) as an **onedir** build: the exe
  plus its `_internal` folder. Ship the whole folder; the exe alone does not run.
- Connects to a **local AI** through an **OpenAI-compatible** endpoint (default `http://127.0.0.1:8080/v1`,
  the user's existing Strata service). No cloud fallback (`ALLOW_CLOUD_AI = False`).
- Search: the first-party YouTube channel **@ranai-korea** is searched first and is the top-weighted,
  user-verified source; then GitHub and Hugging Face.
- For a selected result it acquires the source, researches documented install methods with evidence
  (repo files plus the selected own post), and builds an installation plan. Plans stop at
  `READY_FOR_APPROVAL` or `REVIEW_REQUIRED` (or `BLOCKED`). Nothing installs without explicit user approval.
- Runtime data lives in `%LOCALAPPDATA%\Ornith-Solution-Scout` (`runtime_paths.py`), separate from packaged resources.

## Module map (repo root, flat layout)

| File | Role |
|---|---|
| `app.py` | Entry point (`main()`): CLI flags (`--dry-run`, `--developer`, `--build-info-output`, `--acceptance-*`), loading window, Product Core controller. |
| `normal_ui.py` | Normal-user Tk UI over the Product Core controller: search, result cards, first-party ordering (`_prefer_relevant_first_party`), Local AI settings, window fitting. |
| `solution_scout.py` | Evidence-first search/scouting with human phase gates; first-party YouTube index, GitHub and HF search, `SOURCE_PRIORITY`. |
| `query_planner.py` | Conservative local-only search-query planning. |
| `install_research.py` | Evidence-bound install research on acquired source trees; Local AI extraction with provenance checks; deterministic first-party fallback (`_finish_without_local_ai`). |
| `installation_evidence_coverage.py` | Bounded main-install evidence projection (e.g. Dockerfile COPY coverage); never an installation authority. |
| `installation_plan.py` | Deterministic, evidence-bound plan construction; `EXECUTABLE_TYPES` (Python/conda/model/installer-script only; no npm/Docker step type). |
| `plan.py`, `executor.py`, `resume.py`, `preflight.py` | Approval-gated plans, atomic executor for approved steps only, safe resume, read-only environment inspection. |
| `local_ai_assist.py` | Optional, bounded AI advice; search and install authority stay outside it. |
| `local_ai_config.py` | Validated Local AI settings and hash-bound per-work snapshots. |
| `local_only_test.py` | OpenAI-compatible model gate/client (retries once without `reasoning_effort` on a 400/422 that names it). |
| `first_party_identity.py` | Authority for identifying the configured first-party channel. |
| `firstparty_research_evidence.py` | Immutable evidence from the selected own post, kept distinct from verified repo files. |
| `product_config.py` | `APP_NAME`, **`APP_VERSION`**, channel handle/URL, Local AI defaults, public source list. |
| `win11_theme.py` | Win11-style theme; `usable_screen_area()` (work area, excludes taskbar) and `place_window()`. |
| `localization.py` + `locales/*.json` | JSON localization: `en`, `zh-CN`, `zh-TW`, `ja`, `es`, `ko`. |
| `data/` | Bundled `first_party_youtube_index.json`, `curated_sources.json`, `radar_sources.json`. |
| `build_identity.py`, `BUILD_INFO.json` | Build identity of the running program / source fingerprint. |
| `*_acceptance*.py`, `acceptance_runner.py` | Opt-in isolated acceptance runs; never install or touch production records. |
| `test_*.py`, `test_fixtures/` | unittest suites; `test_fixtures/local_ai_on/` holds recorded offline fixtures. |

## Run from source (Windows, PowerShell)

```powershell
py -3.10 app.py              # normal UI
py -3.10 app.py --dry-run    # isolated, no-web fixture workspace
```

## Tests

```powershell
py -3.10 -m unittest test_local_ai_on_regressions test_channel_relevance test_links_scroll -v
py -3.10 -m unittest test_channel_live_search -v
```

- `test_local_ai_on_regressions` is offline (fake local OpenAI-compatible server + recorded fixtures).
- `test_channel_live_search` reads `..\evidence\network\original-posts.html` (and other files under
  `..\evidence\`) relative to the parent of the repo root. Those files are not in the repo; without them the
  suite errors. That error is expected and does not mean a regression.
- `test_local_ai_on_regressions` and `test_channel_live_search` create real Tk windows and need a display
  (a desktop session; on Linux CI use e.g. Xvfb).
- Run the full suite before and after every change and report the counts.

## Build (Windows)

```powershell
& .\build\build-release.ps1 -Python C:\path\to\python310\python.exe -AppVersion 0.1.19 -OutputRoot C:\new\empty\output
```

- The `build\` folder (with `build-release.ps1`) is **not** in the source zip; copy it from the previous source
  install. See `BUILDING.md`.
- Use an existing CPython 3.10.11 with PyInstaller 6.22.3 and packaging 26.3; do not install dependencies as part of a build.
- The version lives in `product_config.APP_VERSION`. Keep `-AppVersion` equal to it. Renaming the zip or exe
  is not enough to change the version.
- `BUILD_INFO.json` binds a build to the exact root Python sources (SHA-256 per file + `source_fingerprint`).
  The build regenerates it, so never hand-edit it. The copy in the source tree may lag (it can still say 0.1.18).
- Output to a new folder and deliver the whole onedir folder, including `_internal`.

## Invariants and safety rules (do not break)

1. **Own channel first.** Relevant @ranai-korea posts rank first, with Local AI **on or off**. When an own
   post is selected, its documented install method is the primary method; README alternatives (e.g. Docker) are only optional.
2. **Local AI never makes results worse.** Every Local AI failure (bad settings, unreachable, model not
   found, HTTP error, timeout, truncated/invalid output) must degrade to the deterministic, AI-off path. Never
   block search with a popup; never return BLOCKED just because the AI failed. Keep the deterministic fallback.
3. **No auto-execution.** Research and planning are read-only. Never run install commands (pip, conda, npm,
   docker, scripts) without an explicit approval gate. Do not add new executable step types (for example npm)
   without the owner's consent.
4. **Keep provenance validation.** AI claims must cite evidence that really supports them. Reject only the
   unsupported item, not the whole response, but never accept unsupported claims.
5. **Preserve line endings.** The repo mixes CRLF and LF files (e.g. `normal_ui.py`, `app.py`, `locales/*.json`
   are CRLF; `local_ai_assist.py`, `win11_theme.py` are LF). Keep each file's existing style and avoid whole-file
   rewrites that produce noisy diffs.
6. **Locales in sync.** Any new or changed UI string goes into all six `locales/*.json` files with the same key.
7. **Work on a copy.** Make changes in a new folder or branch. Never overwrite the source tree, the original
   candidate, or the production install/release.
8. **No publishing without approval.** Do not tag, release, push to the main branch, or replace a production
   build unless the owner (xiang ran) explicitly approves.
9. No cloud AI, no model-weight downloads during research. HF code reads metadata and documents only.

## Working method for bugs (TDD)

1. **Reproduce**: state the hypothesis and reproduce it offline (recorded fixtures, fake local AI server).
2. **Red test first**: add a regression test (e.g. in `test_local_ai_on_regressions.py`) and confirm it
   **fails on the unfixed code** for the stated reason.
3. **Fix**: make the smallest change that turns it green, without weakening the invariants above.
4. **Verify**: run the whole suite; all old tests must still pass (apart from the known evidence-dependent errors).
5. **Review**: re-read the diff for regressions in AI-off behaviour, ranking, provenance and line endings. Then
   summarise the root cause, the change, and the test results. List anything left unverified (e.g. real-model behaviour).
