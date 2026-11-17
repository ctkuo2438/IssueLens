# Evidence index

This folder holds the verification records, raw model output, sources, reviews, and screenshots for M1, M2, M3, and M5. The [M1](../docs/m1.md), [M2](../docs/m2.md), [M3](../docs/m3.md), and [M5](../docs/m5.md) guides are the reference for features and current status. This index helps you find the evidence. It does not restate historical results as the latest acceptance.

## Latest results

Entries are grouped by milestone, newest first.

### M5

**Packaging step 5: public release.** IssueLens is public at <https://github.com/ctkuo2438/IssueLens> under the MIT license and tagged `v0.1.0`. Before the push, every commit was checked for keys. The history was rewritten to replace author emails with the GitHub noreply address and to remove Codex session IDs from an early commit. Installing from GitHub into a fresh HOME: the first start with `@v0.1.0` took 10.7 seconds, over the 10-second Codex default, and later starts took 1.5 seconds. Every start contacts GitHub, so the server cannot start offline. The probe ran the handshake, listed the tools, and made one tool call, which stopped at the missing-key check. No model or GitHub API call was made. [GitHub install check](m5/github-install.json)

**Packaging step 3: installed version in both clients.** Claude Code 2.1.325 and Codex 0.169.0 each started `issuelens-mcp` installed with `uvx` from the local repository, read settings from `~/.config/issuelens/.env`, and ran one real analysis of `pallets/flask#6146`. Both used `gpt-5.6` / `medium`, showed the full report, and ran no commands. Claude Code's run was `completed`. Codex's run was `partial` because one search read only its first page. The report contents still await human review. [Acceptance summary](m5/installed-live/20261115T205301Z/summary.json)

**Packaging step 2: settings without a checkout.** The installed version no longer depends on a checkout. Environment variables take precedence, and everything else is read from `~/.config/issuelens/.env`. A probe showed that Claude Code passes all shell variables, while Codex passes only 9 by default, such as `HOME` and `PATH`, so settings moved to a file in the home directory. Four cases of the installed version were verified with a scratch HOME and fake keys. No model or GitHub call was made. [Settings check](m5/settings.json)

**Packaging step 1: build a package.** IssueLens can be built as a package and provides the `issuelens-mcp` command. The wheel contains only the 31 files from `backend/`. After installing with `uvx` in a fresh scratch HOME, the server completed the handshake and listed its tools. No model or GitHub call was made. Reading keys was left for step 2. [Packaging check](m5/packaging.json)

**Context budget change.** The `AGENT_CONTEXT_TOKENS` default went from 12,000 to 24,000, and `pallets/flask#6146` was rerun with the real OpenAI and GitHub APIs. The result went from `partial` to `completed`: no excerpt was omitted, and the latest maintainer comment, S5, was cited. Usage was almost the same as at 12,000. The largest request this time, including reserved space, was 11,130 tokens, and the run took a different path from before, so we cannot say that 12,000 would certainly fail. The report contents still await human review. [Acceptance summary](m5/context-budget-24000/20261113T220623Z/summary.json)

**Claude Code live acceptance.** The user analyzed the same `pallets/flask#6146` in an interactive Claude Code 2.1.323 session. The tool call finished in about 22 seconds, and IssueLens sent 14 progress notifications. The report Claude Code received was identical to the one the server sent. Claude correctly relayed the `partial` report and its 8 sources and ran no commands. The session used auto permission mode, so there was no approval prompt. How the interface displays progress was not recorded. The report contents still await human review. [Acceptance summary](m5/claude-code-live/20261113T202631Z/summary.json)

**Codex live acceptance.** Codex 0.167.2 analyzed `pallets/flask#6146` with the real OpenAI and GitHub APIs. The tool call finished in about 20 seconds, and IssueLens sent 12 progress notifications. Codex correctly relayed the `partial` report and its 7 sources and ran no commands. `codex exec --json` does not show progress. The report contents still await human review. [Acceptance summary](m5/codex-live/20261113T031334Z/summary.json)

**Step 1: client handshake.** The local MCP server completed a handshake with the real Claude Code 2.1.323 and Codex 0.167.2, and both got `analyze_github_issue`. Claude Code first tried `server/discover` from the 2026-07-28 protocol, then fell back to `2025-11-25`. Codex used `2025-06-18` directly. Both clients used scratch settings. No analysis ran, and no model or GitHub call was made. [Client connection check](m5/client-handshake.json)

### M3

**Unreadable records.** The check used a temporary PostgreSQL cluster. Before the fix, a queued record whose snapshot or Trace no longer passed validation stopped the Worker. After the fix, that record is marked `failed`, its original content is saved separately in `incompatible_record`, and analyses queued after it complete normally. Startup also no longer fails on an unreadable interrupted record. No model or GitHub call was made. [Unreadable records check](m3/incompatible-records.json)

**Database reconnect.** The check used a temporary PostgreSQL cluster and did not touch the project database. With default settings, the database was down for 48 seconds, and the API returned 503 during that time. When the database came back, the API returned to normal right away, and the Worker was ready 3.1 seconds later in the same process. If the Worker loses the connection during an analysis, it reacquires the lock and finishes the same analysis from its checkpoint. Other storage errors stop the Worker. The record also lists the issues found in two rounds of code review and their fixes. No model or GitHub call was made. [Database reconnect check](m3/database-reconnect.json)

**Trace.** The check used a real PostgreSQL database and Worker processes. After the Worker was replaced, the same job ID showed the original attempt, the retry link, and the stop reason. A test interrupted during a tool call also confirmed that durations that were not saved stay empty. Model and GitHub responses were simulated. [Trace check](m3/trace.json)

**Bounded retry.** The check covered transient model and GitHub failures, budget exhaustion, and permanent errors. With a real PostgreSQL database and Worker processes, it verified that a replacement Worker respects the saved retry time and completes under the original job ID. Model and GitHub responses were simulated. [Bounded retry check](m3/bounded-retry.json)

**Checkpoint.** With a real PostgreSQL database and Worker processes, the check stopped the Worker during a label read, once gracefully and once by force. The replacement process resumed from the saved model and tool state and completed both analyses under the same job ID. Model and GitHub responses were simulated. [Checkpoint check](m3/checkpoint.json)

**Background Worker.** With real processes and PostgreSQL, the check force-restarted the API. The same Worker kept running the analysis, and the new API resumed replaying events and read back the same result. The analysis used simulated responses. [Background Worker check](m3/background-worker.json)

**Step 1: live persistence.** One analysis with the real model and GitHub checked that after a restart the original URL, the full JSON, the 8 sources, and the 35 Activity entries were unchanged. [Live restart check](m3/live-persistence/20261110T220602_439770Z/report.json) / [Screen after restart](m3/live-persistence/20261110T220602_439770Z/after-restart.jpg)

The 280 checks and the simulated analysis from that time are recorded separately. [Step 1 implementation check](m3/postgresql.json)

### M2 frozen acceptance

M2 is frozen. The acceptance ran once each on HTTPX #3602, pytest #14560, and Ruff #29177 with `gpt-5.6` / `medium` and the original budgets. All three cases have a final answer, and one is partial. Under the predefined criteria, this round's limited functional acceptance passed. User review is still pending.

- [Latest reading version: results, limits, and raw drafts](m2/acceptance/20261110T065740_115528Z/review.md)
- [Raw API run report](m2/acceptance/20261110T065740_115528Z/report.json) / [Assistant source check](m2/acceptance/20261110T065740_115528Z/assistant-review.json)
- [Predeclared protocol](m2/acceptance/20261110T065740_115528Z/protocol.json) / [Case criteria](m2/acceptance/20261110T065740_115528Z/cases.json) / [Independent selection](m2/acceptance/20261110T065740_115528Z/selection.json)

This round had no answer repair, template lookup, or README lookup events. Do not read the limited pass as proof that every branch was tested, that quality is stable, or of production accuracy.

## Folder layout

```text
evidence/
  README.md
  m1/
    verification.json
    live-analysis.jpg
  m2/
    steps/
    screenshots/
    evaluations/
      <UTC timestamp>/
    acceptance/
      <UTC timestamp>/
  m3/
    *.json
    live-persistence/
      <UTC timestamp>/
  m5/
    *.json
    <run>/
      <UTC timestamp>/
```

| Location | What it holds | How to read it |
| --- | --- | --- |
| [M1 check](m1/verification.json) / [screenshot](m1/live-analysis.jpg) | The basic flow and one real browser check. | An observation that the features connect, not a quality benchmark. |
| `m2/steps/` | Check summaries, fingerprints, and observations for each development step. | A file may record both offline simulations and real API calls. Each file says which is which. |
| `m2/screenshots/` | Screens from real feature checks and clearly labeled offline screens. | Offline screens are not evidence of model quality. |
| `m2/evaluations/` | Real model runs on five fixed fictional GitHub snapshots. Only the latest round is kept. | They include no real GitHub network latency and do not measure search recall. |
| `m2/acceptance/` | Acceptance runs and reviews with the real model and real GitHub. Only the M2 frozen acceptance is kept. | A completed run is not a semantic pass. |
| `m3/` | Check records from PostgreSQL persistence and reliability development. `live-persistence/` holds the step 1 live restart check. | Real database results and simulated model output are labeled separately. They do not fill in M2 quality scores. |
| `m5/` | Records of the MCP server's connections to Claude Code and Codex and of real analyses. Each real run has its own folder, such as `codex-live/<UTC timestamp>/`. | Observations of client integration, not evidence of analysis quality. |

Timestamped folders use UTC. A folder named `20261110...` may still be 2026-11-09 in local time. `scripts/evaluate.py` writes new runs to `m2/evaluations/<UTC timestamp>/`, and `scripts/acceptance.py` writes them to `m2/acceptance/<UTC timestamp>/`.

## Model evaluation on fixed fictional snapshots

| UTC run folder | Automatic result | Observations and link |
| --- | --- | --- |
| `20261109T083809_862702Z` | 5/5 | Regression after the template feature. The fixed data has no templates, so it cannot accept template quality. [Report](m2/evaluations/20261109T083809_862702Z/report.json) |

5/5 is the number of cases that matched the automatic checklist in that report. It is not a quality score on a common scale, and it still awaits user review. One run with all five cases matching does not close the variability in which the PNG case recommends an extra `needs-info`. For the four earlier rounds and their re-scoring under common rules, see the [M2 history evidence index](../docs/m2.md#history) and the [common-rule comparison](m2/steps/behavior.json).

## M2 development checks

| Topic | Record |
| --- | --- |
| Similar issues | [similar-issues.json](m2/steps/similar-issues.json) |
| Label recommendations | [labels.json](m2/steps/labels.json) |
| Five-case evaluation script | [evaluation.json](m2/steps/evaluation.json) |
| Classification / reproduction attribution | [behavior.json](m2/steps/behavior.json) |
| Templates | [templates.json](m2/steps/templates.json) |
| Maintainer voice in drafts | [draft-style.json](m2/steps/draft-style.json) |
| First Context Builder | [context.json](m2/steps/context.json) |
| Editable draft | [editable-draft.json](m2/steps/editable-draft.json) |
| First live acceptance | [live-acceptance.json](m2/steps/live-acceptance.json) |
| Comment reads | [comments.json](m2/steps/comments.json) |
| PR label scope | [label-scope.json](m2/steps/label-scope.json) |
| Label wording consistency | [label-consistency.json](m2/steps/label-consistency.json) |
| Citations / answer repair | [answer-repair.json](m2/steps/answer-repair.json) |
| Draft history attribution, first round / follow-up | [draft-grounding.json](m2/steps/draft-grounding.json) / [draft-grounding-final.json](m2/steps/draft-grounding-final.json) |
| Minimum required lookups | [required-lookups.json](m2/steps/required-lookups.json) |
| Draft actions / closed status | [draft-status.json](m2/steps/draft-status.json) |
| Live regression after the action rules | [draft-status-live.json](m2/steps/draft-status-live.json) |
| Reasoning settings | [reasoning-settings.json](m2/steps/reasoning-settings.json) |

These summaries record the test counts and code fingerprints from the time of each development step. For the latest pre-freeze record of 262 tests and Pyright, see [M2 verification](../docs/m2.md#verification). The early summaries are not rewritten as new results.

## Screenshots

| Screenshot | Type and source |
| --- | --- |
| [M1 basic analysis](m1/live-analysis.jpg) | Real GitHub and model feature check. |
| [Similar issues](m2/screenshots/live-comparison.jpg) / [Labels](m2/screenshots/live-labels.jpg) / [Templates](m2/screenshots/live-templates.jpg) | Screens from real feature checks, not a quality benchmark. |
| [YAML fields](m2/screenshots/template-yaml-offline.jpg) | Fictional form, offline component check. |
| [Draft on desktop](m2/screenshots/editable-draft-offline.jpg) / [on mobile](m2/screenshots/editable-draft-mobile-offline.jpg) | Fictional data, simulated model, and interaction check. |

<a id="retention"></a>
## Retention

The working tree keeps only the **current** evidence for each milestone. A cleanup on 2026-11-15 deleted 80 files that later results had superseded:

- Ten older rounds of the three-case live acceptance from M2 development (Flask #5804, #4032, Werkzeug #2948). The final frozen acceptance is kept.
- Four earlier rounds of the five-case evaluation. The latest round is kept.
- Two finished model experiments and their two summaries.
- The `manifest.json` that mapped moved paths, and the three protocol, case, and selection files in `evaluation/` that were byte-for-byte identical to the frozen acceptance copies.

These files still exist in full in commit `619ed99`, where the old layout is unchanged. For example, run `git show 619ed99:evidence/manifest.json`, or bring back a whole directory with `git restore --source 619ed99 -- evidence/model-comparisons`. `git restore` puts the files back at their old paths. The [M2 history evidence index](../docs/m2.md#history) summarizes the key observations from each round. Kept JSON files may mention deleted paths or paths from before a move. They stay as they were recorded and are not rewritten.

On 2026-11-16, before the repository went public, the git history was rewritten. Every commit's author email became the GitHub noreply address, and 4 Codex session IDs were removed from the first Codex live acceptance commit (later commits had already removed them). As a result, every commit ID changed. The docs use the new IDs. Kept evidence still records the old IDs from that time. For example, `6da82a3` in the installed-version acceptance summary is now `991ce41`.

Also on 2026-11-16, the directories were moved under their milestone folders: `evidence/m2/verification/` to `evidence/m2/steps/`, `evidence/evaluations/` to `evidence/m2/evaluations/`, `evidence/live-acceptance/` to `evidence/m2/acceptance/`, `evidence/m3/verification/` to `evidence/m3/`, and `evidence/m5/verification/` to `evidence/m5/`. The file contents were not changed, so kept JSON files may still mention the old paths.

To add evidence later, put full run results in a timestamped folder and manual verification summaries and screenshots in the matching milestone folder, then update this index. When a new result supersedes an old one, you may delete the old raw output. Leave a summary of its observations in the relevant doc and note which commit it can be restored from. Do not merge raw JSON files into one, overwrite a kept report, or present a human-edited draft as the model's original output.
