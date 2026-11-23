# Evidence index

This folder keeps the records behind the milestone guides: verification summaries, raw reports, reviews, and screenshots. Each guide's Verification table states the results and links here. This index only says where each record is and what kind of check it is.

**Kinds of check.** *Offline* checks simulate the model and GitHub. *Real PostgreSQL* checks use a real database with simulated model and GitHub responses. *Real model and GitHub* runs call both APIs. *Real client* runs go through Claude Code or Codex. A completed run is not a semantic pass: every analysis report still awaits human review.

Timestamped folders use UTC, so a folder named `20261110...` may still be 2026-11-09 in local time.

## M1

| Record | Check | Kind |
| --- | --- | --- |
| [verification.json](m1/verification.json), [live-analysis.jpg](m1/live-analysis.jpg) | One analysis of `pallets/flask#6146` in the browser, refresh, and layout | Real model and GitHub |

## M2

M2 is frozen. Start with the [frozen acceptance review](m2/acceptance/20261110T065740_115528Z/review.md).

| Record | Check | Kind |
| --- | --- | --- |
| [acceptance/20261110T065740_115528Z/](m2/acceptance/20261110T065740_115528Z/review.md) | Frozen acceptance on HTTPX #3602, pytest #14560, and Ruff #29177: [review](m2/acceptance/20261110T065740_115528Z/review.md), [raw report](m2/acceptance/20261110T065740_115528Z/report.json), [assistant source check](m2/acceptance/20261110T065740_115528Z/assistant-review.json), [protocol](m2/acceptance/20261110T065740_115528Z/protocol.json), [cases](m2/acceptance/20261110T065740_115528Z/cases.json), [selection](m2/acceptance/20261110T065740_115528Z/selection.json) | Real model and GitHub |
| [evaluations/20261109T083809_862702Z/](m2/evaluations/20261109T083809_862702Z/report.json) | Latest run on five fixed fictional snapshots | Real model, fictional GitHub data |
| [server-reads/](m2/server-reads/) | After the freeze, with the server reads: HTTPX #3602 at 12,000 before ([20261123T194608Z](m2/server-reads/20261123T194608Z/report.json)) and after ([20261123T200214Z](m2/server-reads/20261123T200214Z/report.json)) the optional-tool reserve fix; pytest #14560 ([20261123T202838Z](m2/server-reads/20261123T202838Z/report.json)) and Ruff #29177 ([20261123T202942Z](m2/server-reads/20261123T202942Z/report.json)) at 12,000; pytest at 24,000 ([20261123T203738Z](m2/server-reads/20261123T203738Z/report.json)) | Real model and GitHub |
| [steps/](m2/steps/) | One summary per development step, with the test counts and code fingerprints of that time (table below) | Mixed; each file says which parts were real |
| [screenshots/](m2/screenshots/) | `live-*.jpg` from real feature checks; `*-offline.jpg` with fictional data and a simulated model | Mixed, by file name |

| Step | Record |
| --- | --- |
| Similar issues | [similar-issues.json](m2/steps/similar-issues.json) |
| Label recommendations / PR label scope / wording consistency | [labels.json](m2/steps/labels.json) / [label-scope.json](m2/steps/label-scope.json) / [label-consistency.json](m2/steps/label-consistency.json) |
| Templates | [templates.json](m2/steps/templates.json) |
| Five-case evaluation script / classification and reproduction rules | [evaluation.json](m2/steps/evaluation.json) / [behavior.json](m2/steps/behavior.json) |
| First Context Builder | [context.json](m2/steps/context.json) |
| Comment reads / minimum required lookups | [comments.json](m2/steps/comments.json) / [required-lookups.json](m2/steps/required-lookups.json) |
| Citations and answer repair | [answer-repair.json](m2/steps/answer-repair.json) |
| Draft voice / history attribution (first, follow-up) / actions and closed status | [draft-style.json](m2/steps/draft-style.json) / [draft-grounding.json](m2/steps/draft-grounding.json), [draft-grounding-final.json](m2/steps/draft-grounding-final.json) / [draft-status.json](m2/steps/draft-status.json) |
| Editable draft | [editable-draft.json](m2/steps/editable-draft.json) |
| Live regressions: first acceptance / after the action rules | [live-acceptance.json](m2/steps/live-acceptance.json) / [draft-status-live.json](m2/steps/draft-status-live.json) |
| Reasoning settings | [reasoning-settings.json](m2/steps/reasoning-settings.json) |

## M3

| Record | Check | Kind |
| --- | --- | --- |
| [postgresql.json](m3/postgresql.json) | Step 1: PostgreSQL records | Offline and real PostgreSQL |
| [live-persistence/20261110T220602_439770Z/](m3/live-persistence/20261110T220602_439770Z/report.json) | One analysis survives an app restart: same URL, JSON, 8 sources, 35 Activity entries ([screen after restart](m3/live-persistence/20261110T220602_439770Z/after-restart.jpg)) | Real model and GitHub |
| [background-worker.json](m3/background-worker.json) | The Worker keeps running while the API restarts | Real PostgreSQL |
| [checkpoint.json](m3/checkpoint.json) | A replacement Worker resumes from the checkpoint | Real PostgreSQL |
| [bounded-retry.json](m3/bounded-retry.json) | Retries stay within the original budgets across a Worker replacement | Real PostgreSQL |
| [trace.json](m3/trace.json) | Trace shows attempts, retry links, and stop reasons | Real PostgreSQL |
| [database-reconnect.json](m3/database-reconnect.json) | API and Worker recover after a 48-second database outage | Real PostgreSQL |
| [incompatible-records.json](m3/incompatible-records.json) | An unreadable record is marked `failed` instead of stopping the Worker | Real PostgreSQL |

M4 has no evidence folder. Its checks are offline and PostgreSQL tests linked from the [M4 guide](../docs/m4.md), plus one real GitHub sign-in recorded only in the message of commit `e2c63f8`.

## M5

| Record | Check | Kind |
| --- | --- | --- |
| [client-handshake.json](m5/client-handshake.json) | Claude Code 2.1.323 and Codex 0.167.2 connect and list the tool | Real client, no analysis |
| [codex-live/20261113T031334Z/](m5/codex-live/20261113T031334Z/summary.json), [claude-code-live/20261113T202631Z/](m5/claude-code-live/20261113T202631Z/summary.json) | One analysis from each client at the 12,000 budget: summary, report, sanitized protocol log, final message | Real client |
| [context-budget-24000/20261113T220623Z/](m5/context-budget-24000/20261113T220623Z/summary.json) | The same issue at the 24,000 budget | Real model and GitHub |
| [packaging.json](m5/packaging.json) | Wheel contents and a clean `uvx` install | Real install, no analysis |
| [settings.json](m5/settings.json) | An installed copy reads `~/.config/issuelens/.env` after the environment | Real install, fake keys |
| [installed-live/20261115T205301Z/](m5/installed-live/20261115T205301Z/summary.json) | One analysis from each client with the installed package; [claude-code/](m5/installed-live/20261115T205301Z/claude-code/report.json) and [codex/](m5/installed-live/20261115T205301Z/codex/report.json) each hold the report, sanitized protocol log, and final message | Real client |
| [github-install.json](m5/github-install.json) | Release checks and installing `@v0.1.0` from GitHub | Real install, no analysis |
| [github-live/20261117T010102Z/](m5/github-live/20261117T010102Z/summary.json) | One analysis through the `@v0.1.0` GitHub install, driven by a probe script: summary, report, protocol log | Real model and GitHub |
| [release-live/20261123T230815Z/](m5/release-live/20261123T230815Z/summary.json) | The same analysis from the checkout before tagging `v0.2.0`, driven by a probe script: summary, report, protocol log | Real model and GitHub |
| [github-install-v0.2.0.json](m5/github-install-v0.2.0.json) | Installing `@v0.2.0` from GitHub with an empty uv cache, then a second start, and listing the tool | Real install, no analysis |
| [claude-code-v0.2.0/20261123T234521Z/](m5/claude-code-v0.2.0/20261123T234521Z/summary.json) | Claude Code drives the `@v0.2.0` GitHub install for one analysis: the pasted final message, the JSON report from the session transcript, and a summary with an assistant source check | Real client |

<a id="retention"></a>
## Retention

The working tree keeps only the evidence behind each milestone's current state. To add evidence, put a run with several files in a `<name>/<UTC timestamp>/` folder under its milestone, put single summaries and screenshots directly in the milestone folder, and add a row here. When a newer result supersedes an old one, the old raw output may be deleted if the guide keeps its observations and names a commit that still has the files. Never rewrite a kept report or present an edited draft as model output.

Kept files are left as recorded, so they may mention paths or commit IDs that later changed:

- **2026-11-15 cleanup.** 80 superseded files were deleted: ten old M2 acceptance rounds (Flask #5804, #4032, Werkzeug #2948), four earlier five-case evaluations, two finished model experiments with their summaries, `manifest.json`, and three files in `evaluation/` that duplicated the frozen acceptance. The [M2 history](../docs/m2.md#history) keeps their observations. Commit `619ed99` still has them in the old layout: run `git show 619ed99:evidence/manifest.json`, or `git restore --source 619ed99 -- evidence/model-comparisons` to put a directory back at its old path.
- **2026-11-16 history rewrite.** Before the first push, every commit's email became the GitHub noreply address, and 4 Codex session IDs were removed from the first Codex live run commit. Every commit ID changed. The guides use the new IDs, while kept files keep the old ones; for example, `6da82a3` in the installed-live summary is now `991ce41`.
- **2026-11-16 folder move.** `m2/verification/` became `m2/steps/`, `evaluations/` became `m2/evaluations/`, `live-acceptance/` became `m2/acceptance/`, and `m3/verification/` and `m5/verification/` were flattened into `m3/` and `m5/`. File contents did not change.
