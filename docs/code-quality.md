# Code quality review

In November 2026 the codebase was reviewed one layer at a time, following the inverted pyramid in Lauren Tan's AI Engineer talk "All Code Is Slop (Except Mine)". From the widest layer to the narrowest, the layers are:

1. The code's own design: architecture and API.
2. Static checks: lint, types, and tests.
3. Runtime verification.
4. Written rules.
5. Human review.

Each layer catches what the one above lets through. The more the upper layers check mechanically, the less depends on review.

Layer 1, architecture and API design, has six of its seven items done. Layer 2 adds Python lint with complexity and length limits, and ten functions remain marked as exceptions. Splitting them in batches 2 to 4 is paused, and item 7, frontend lint and layers 3 to 5 have not started. Every change kept the API, the `JobSnapshot` contract, the model's prompts and tools, and `checkpoint_signature` unchanged.

## Use

Run these checks after a code change:

```bash
uv run --locked python -W error::RuntimeWarning -m unittest discover -s tests -q
uv run --locked ruff check
pyright --pythonpath .venv/bin/python backend evaluation scripts
npm --prefix frontend run typecheck
```

After changing a model that the API returns, regenerate the frontend's types:

```bash
uv run --locked python scripts/export_openapi.py
npm --prefix frontend run api:types
```

## Layer 1: architecture and API design

| Item | Problem | Change | Commit |
| --- | --- | --- | --- |
| 1. Test-only storage mode | `create_app(store=...)` built a third execution mode that production never used, and only tests injected it. | Removed `AnalysisStore` and its fallbacks. A new integration test covers the 503 during a real database outage. | `0798215` |
| 2. One job interface | The routes branched on `isinstance(manager, PostgresJobQueue)` four times. | `JobService` is a Protocol. `MemoryJobService` and `PostgresJobQueue` implement it, and the event route has one loop for both modes. | `f3b68ba` |
| 3. One lifecycle | Five places assigned statuses directly, and nothing said which changes were allowed. | `lifecycle.py` holds `ALLOWED_TRANSITIONS`, and `start`, `finish` and `record` are the only ways to change a status or add an event. A test fails if another module assigns `.status`. | `a0674d6` |
| 4. Tool registry | Adding a tool took five edits that had to agree, and `GitHubToolState` copied 24 fields by hand. | Each tool is one `ToolSpec`, `TOOLS` and dispatch are derived from them, and `GitHubTools` inherits its state dataclass. | `d95353e` |
| 5. Agent loop | `_investigate` was a 340-line method with complexity 45. | `plan_request` decides what each request may do without changing state, and returns a frozen `RequestPlan`. The loop calls named steps, and its complexity is 14. | `c3b2832` |
| 6. Frontend types | The frontend copied about 90 lines of API types by hand, looser than the backend's. | `frontend/openapi.json` is exported from the API, and `openapi-typescript` generates `frontend/app/openapi.ts`. `ApiModel` marks fields with defaults as required in responses. | `9753c52` |
| 7. Smaller items | `page.tsx` is one large component, DELETE means cancel, SSE polls, the MCP schema is duplicated, some comments repeat the code, and `quality.py` relies on regexes. | Not started. | - |

## Layer 2: static checks

### ruff settings

`pyproject.toml` sets:

| Setting | Effect |
| --- | --- |
| `select = ["F", "C901", "PLR0915", "RUF100"]` | Only these rules run. The explicit list replaces ruff's broader default set, so a ruff upgrade cannot enable rules on its own. |
| `F` | Pyflakes: unused imports and variables, undefined names, malformed format strings, and statements that are always wrong. |
| `C901` with `max-complexity = 12` | A function has at most 12 branches (cyclomatic complexity). |
| `PLR0915` with `max-statements = 50` | A function has at most 50 statements, pylint's default. It catches long functions with few branches. |
| `RUF100` | Reports a `noqa` marker that is no longer needed, so a function that is split below a limit loses its exception. |

ruff is a development dependency pinned in `uv.lock`. It checks the repository in about 0.05 seconds, compared with about 7 for pylint. Pyright already covers the cross-module inference that pylint adds.

### Exceptions

Split a function that exceeds a limit. If splitting would harm the code's structure, mark it with `# noqa: <code> - <reason>` instead, and name the new exception in your report. `AGENTS.md` asks agents to follow the same rule.

The functions that exceeded a limit when it was set are marked `predates the lint limits`. Ten remain:

| Function | Limits | Plan |
| --- | --- | --- |
| `main.py` `create_app` | C901, PLR0915 | Keep. The score is the sum of its nested route handlers, and each handler is under the limits. |
| `agent.py` `_investigate` | C901, PLR0915 | Keep. Its remaining branches are the loop's early exits and continues. |
| `scripts/evaluate.py` `main`, `scripts/acceptance.py` `main` | PLR0915 | Keep. They are frozen M2 runners. |
| `agent.py` `snapshot`, `agent.py` `_execute_tool` | C901 | Batch 2, paused. |
| `jobs.py` `_run` | C901, PLR0915 | Batch 3, paused. |
| `context.py` `build` | C901, PLR0915 | Batch 4, paused. |
| `tests/test_checkpoint.py` `check_restart`, `run_fixture_worker` | C901, PLR0915 | Last, or keep. |

The four functions planned to be kept still carry `predates the lint limits`. Their markers should be changed to the reasons above.

Batch 1 (`a3e58b2`) split three functions:

| Before | After |
| --- | --- |
| `github.py` `_get` | `_get` sends the request, and `_raise_for_status` turns a status into a refusal, a retry or a cooldown. |
| `github.py` `get_issue_templates` | `_template_files` filters the directory listing, and `_read_template` reads one file and checks its size, identity, source URL and encoding. |
| `templates.py` `parse_template` | `_read_markdown`, `_read_form`, `_form_field` and `_required_options` each read one part of a template. |

## How refactors were verified

The agent loop split and lint batch 1 had to keep behavior identical. Four methods were combined:

- **Tests before the change.** Paths that no test reached got new tests, which had to pass on the old code first. This way they describe the existing behavior.
- **Behavior recording.** A scratch script records every `AgentRun` while the whole suite runs. It captures each event, saved checkpoint and model request, plus the result and the trace. Clock values are normalized, and `PYTHONHASHSEED=0` fixes set order. Two recordings of the old code must match each other before the new code's recording is compared.
- **Differential testing.** For a pure function, the old and new versions run on the same fixture and randomly generated inputs, and every result is compared.
- **Planted bugs.** Each check is shown to fail on a deliberate break. Some breaks pass the tests but not the recording or the comparison, which shows that those two are stricter than the tests.

The recording and comparison scripts were temporary and are not kept.

## Verification

| Check | Kind | Result | Evidence |
| --- | --- | --- | --- |
| Agent loop split | offline recording | 223 runs and 716 model requests were byte-identical before and after the change. Swapping two events changed 19 runs while every test still passed. | Commit message of `c3b2832` |
| Request rules | offline | `plan_request` is tested directly. Two loop paths that no test reached before are covered, and both tests pass on the old code. | [test_request_plan.py](../tests/test_request_plan.py) |
| API types | offline | A stale schema, unregenerated types, a renamed field, a model outside `ApiModel` and a new status each fail a check. | [test_api_contract.py](../tests/test_api_contract.py), commit message of `9753c52` |
| Lint limits | offline | Removing a marker, adding a branch or statements past a limit, and claiming an unneeded exception each fail `ruff check`. | Commit messages of `f6eaf67` and `89f4ebb` |
| Template parser split | offline differential | The old and new parsers agreed on 20,002 templates. A planted note-order change was caught while the template tests passed. | Commit message of `a3e58b2` |
| GitHub status and template reading | offline | The new tests pass on the old code, and six planted bugs each fail one. | [test_github_status.py](../tests/test_github_status.py), [test_templates.py](../tests/test_templates.py) |
| Full suite | real PostgreSQL | Passes with `ISSUELENS_TEST_DATABASE_URL` set, none skipped. | [M3 setup](m3.md#setup) |

## Limits

- Nothing runs the checks automatically. There is no CI and no pre-commit hook, so the checks run only when a person or agent runs them.
- A function marked `noqa` is not checked at all, so it can grow without notice.
- Complexity and statement counts ignore nesting depth. `quality.py`'s `label_consistency_fields` nests five levels deep and still passes both limits.
- ruff's statement count differs slightly from pylint's, for example in nested functions, so counts near 50 can differ by a few.
- These paths still have no test: `_get`'s network errors, a timeout right after a model retry, and a model response without usage.
- The behavior recording reads a local variable named `request` in `agent.py`. A rename breaks the script, not the app.
- The frontend has no lint beyond `tsc`.
- ruff's broader default rule set reports 92 findings, 57 of them import order. It is not enabled.

## Tradeoffs and exercises

- **An explicit rule list versus the tool's defaults:** an explicit list changes only by decision. Defaults follow the tool's latest advice, but an upgrade can fail the checks overnight.
- **Marking existing violations versus fixing them first:** markers let the limits start now and keep the debt visible in the code. A marked function is unchecked until it is split.
- **Recording and differential testing versus unit tests:** the first two prove that nothing changed, including cases nobody thought of, but they also preserve old bugs. Unit tests state the rules that later changes must keep.
- **ruff versus pylint:** ruff is fast and holds every rule in one tool. pylint has more inference-based rules, which Pyright covers here.

Exercise: remove the marker on `agent.py`'s `snapshot` and run `uv run --locked ruff check` to see its score. Restore the marker, move the limitation rules into their own function, and run ruff again. If `snapshot` is now at 12 or below, RUF100 asks you to remove the marker.

## History

- `0798215`: Remove the storage mode that only tests could build.
- `f3b68ba`: Give both storage modes one `JobService` interface.
- `a0674d6`: Route every status change through `lifecycle.py`.
- `d95353e`: Declare each tool and the checkpointed tool state once.
- `9753c52`: Generate the frontend's API types from the backend's schema.
- `c3b2832`: Split the agent loop into named steps and `plan_request`.
- `f6eaf67`: Limit function complexity and length with ruff, marking 13 exceptions.
- `89f4ebb`: Add pyflakes, and ask agents in `AGENTS.md` to run ruff.
- `a3e58b2`: Split `_get`, `get_issue_templates` and `parse_template`, leaving 10 exceptions.

## Next step

Add ESLint to the frontend, so `page.tsx` and its components get limits like the backend's.
