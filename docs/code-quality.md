# Code quality review

In November 2026 the codebase was reviewed one layer at a time, following the inverted pyramid in Lauren Tan's AI Engineer talk "All Code Is Slop (Except Mine)". From the widest layer to the narrowest, the layers are:

1. The code's own design: architecture and API.
2. Static checks: lint, types, and tests.
3. Runtime verification.
4. Written rules.
5. Human review.

Each layer catches what the one above lets through. The more the upper layers check mechanically, the less depends on review.

Layer 1, architecture and API design, has six of its seven items done. Layer 2 adds Python and frontend lint with complexity and length limits. Eight Python functions and the frontend's `Home` remain marked as exceptions. CI runs every check on each push to `main` and each pull request. Batch 2 is done, batches 3 and 4 remain, and item 7 and layers 3 to 5 have not started. Every change kept the API, the `JobSnapshot` contract, the model's prompts and tools, and `checkpoint_signature` unchanged.

## Use

Run these checks after a code change:

```bash
uv run --locked python -W error::RuntimeWarning -m unittest discover -s tests -q
uv run --locked ruff check
pyright --pythonpath .venv/bin/python backend evaluation scripts
npm --prefix frontend run typecheck
npm --prefix frontend run lint
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

### ESLint settings

`frontend/eslint.config.mjs` sets:

| Setting | Effect |
| --- | --- |
| `eslint-config-next/core-web-vitals` | Next.js, React and React Hooks rules, with the rules that affect page loading raised to errors. |
| `eslint-config-next/typescript` | typescript-eslint's recommended rules, such as no unused variables and no `any`. |
| `complexity: 12`, `max-statements: 50` | The backend's limits. ESLint also counts each `&&`, `\|\|`, `??` and `?:` as a branch, so conditions in JSX add up. |
| `reportUnusedDisableDirectives: "error"` | Like `RUF100`: a disable comment that no longer suppresses anything fails the check. |
| `app/openapi.ts` ignored | `openapi-typescript` generates it. |

`npm run lint` passes `--max-warnings 0`, so rules that only warn, such as unused variables, fail it too. `next lint` was removed in Next.js 16, so the script calls the ESLint CLI.

ESLint 10 is pinned in `package-lock.json`. ESLint 9 is no longer supported. Three plugins inside `eslint-config-next` (`react`, `jsx-a11y` and `import`) do not list ESLint 10 as a peer yet, so `npm install` warns `ERESOLVE overriding peer dependency`. Next.js documents ESLint 10 as supported.

### CI

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs two jobs in parallel on each push to `main`, each pull request, and on demand:

| Job | Steps |
| --- | --- |
| `backend` | `uv sync --locked`, the Python suite against a PostgreSQL 14 service container, `ruff check`, and Pyright 1.1.411. The job fails if any test is skipped, so the database checks cannot be dropped silently. |
| `frontend` | `npm ci`, `next typegen`, `npm run typecheck`, and `npm run lint`. A fresh checkout has no `.next`, so `next typegen` writes the route types that `tsc` then validates. |

The container matches the [M3 setup](m3.md#setup): the `issuelens` role, trust authentication, UTF-8 and the C locale. Each action is pinned to a commit SHA, so a moved tag cannot change what runs. The workflow can only read the repository, and the checkout does not keep its token.

### Exceptions

Split a function that exceeds a limit. If splitting would harm the code's structure, mark it with `# noqa: <code> - <reason>` instead, and name the new exception in your report. `AGENTS.md` asks agents to follow the same rule.

The functions that exceeded a limit when it was set are marked `predates the lint limits`. Eight remain:

| Function | Limits | Plan |
| --- | --- | --- |
| `main.py` `create_app` | C901, PLR0915 | Keep. The score is the sum of its nested route handlers, and each handler is under the limits. |
| `agent.py` `_investigate` | C901, PLR0915 | Keep. Its remaining branches are the loop's early exits and continues. |
| `scripts/evaluate.py` `main`, `scripts/acceptance.py` `main` | PLR0915 | Keep. They are frozen M2 runners. |
| `jobs.py` `_run` | C901, PLR0915 | Batch 3. |
| `context.py` `build` | C901, PLR0915 | Batch 4. |
| `tests/test_checkpoint.py` `check_restart`, `run_fixture_worker` | C901, PLR0915 | Last, or keep. |

The four functions planned to be kept still carry `predates the lint limits`. Their markers should be changed to the reasons above.

The frontend marks exceptions with `// eslint-disable-next-line <rule> -- <reason>`. Three lines in `page.tsx` are marked:

| Line | Rule | Plan |
| --- | --- | --- |
| `Home` | `complexity` (125) | Keep while the frontend stays minimal; MCP is the focus. It is the whole page, marked `predates the lint limits`. |
| The mount effect's `setRequestedAnalysis` | `react-hooks/set-state-in-effect` | Keep. The server render cannot read the URL, so the page sets the requested analysis and the sign-in error once after hydration, React's two-pass pattern. |
| The brand link `<a href="/">` | `@next/next/no-html-link-for-pages` | Keep. A full load clears the workspace. A `<Link>` would keep the old analysis on screen, because a client navigation preserves component state. |

Batch 1 (`a3e58b2`) split three functions:

| Before | After |
| --- | --- |
| `github.py` `_get` | `_get` sends the request, and `_raise_for_status` turns a status into a refusal, a retry or a cooldown. |
| `github.py` `get_issue_templates` | `_template_files` filters the directory listing, and `_read_template` reads one file and checks its size, identity, source URL and encoding. |
| `templates.py` `parse_template` | `_read_markdown`, `_read_form`, `_form_field` and `_required_options` each read one part of a template. |

Batch 2 split two `agent.py` methods, each from a complexity of 15:

| Before | After |
| --- | --- |
| `snapshot` (now 5) | `_lookup_limitations` names unfinished comment, search and label lookups; `_record_comparisons` records which candidates the answer compared; `_comparison_limitations` names the similar-issue gaps. They run in the old order, so limitations keep their order. |
| `_execute_tool` (now 9) | `_check_tool_policy` refuses calls the lookup policy forbids, `_exhausted_tool_budget` names the budget a retry would exceed, and `_finish_tool_call` closes the trace attempt and reports the result. |

## How refactors were verified

The agent loop split and lint batches 1 and 2 had to keep behavior identical. Four methods were combined:

- **Tests before the change.** Paths that no test reached got new tests, which had to pass on the old code first. This way they describe the existing behavior.
- **Behavior recording.** A scratch script records every `AgentRun` while the whole suite runs. It captures each event, saved checkpoint and model request, plus the result and the trace. Clock values are normalized, and `PYTHONHASHSEED=0` fixes set order. Two recordings of the old code must match each other before the new code's recording is compared. Each run uses a fresh bytecode cache (`PYTHONPYCACHEPREFIX`): a planted edit that keeps the file's size, made within a second of the last compile, otherwise runs the stale `.pyc`.
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
| Frontend lint | offline | Removing any of the three markers, a stale disable comment, an unused variable, 12 `if`s or 12 `&&`s, 51 statements, a conditional hook and `any` each fail `npm run lint`. Complexity 12 and 50 statements pass. A clean `npm ci` installs from the lockfile. | [eslint.config.mjs](../frontend/eslint.config.mjs), commit message |
| CI | offline simulation | A clean clone with an empty environment passed every CI step against the local PostgreSQL 14: 403 tests with none skipped, ruff, Pyright, typegen, the type check and lint. The skip guard passes `OK` and fails `OK (skipped=28)` and a test failure under the runner's `bash -eo pipefail`. | [ci.yml](../.github/workflows/ci.yml) |
| First CI run | GitHub Actions | The frontend job and the database tests passed. `test_progress_follows_the_activity_log` failed on Python 3.13.14, where one `asyncio.sleep(0)` no longer gave the progress reporter enough loop turns; it passed on the local 3.13.7. The simulated request now waits for the reporter, at most 100 turns. The fixed test passes on both versions, and a reporter that reports the oldest event, or only the final one, fails it on both. | Run 45281201396, [test_mcp.py](../tests/test_mcp.py) |
| Lint batch 2 | offline recording | Five new tests covered the last unreached lines and branches of both methods and passed on the old code. Recordings of the old code matched each other and the new code: 4,450 records over 408 tests. Swapping the two event messages of a finished tool call passed every test but changed the recording; swapping the limitation groups failed a new test and the recording. | [test_comments.py](../tests/test_comments.py), [test_labels.py](../tests/test_labels.py), [test_retry.py](../tests/test_retry.py), [test_checkpoint.py](../tests/test_checkpoint.py) |
| Template parser split | offline differential | The old and new parsers agreed on 20,002 templates. A planted note-order change was caught while the template tests passed. | Commit message of `a3e58b2` |
| GitHub status and template reading | offline | The new tests pass on the old code, and six planted bugs each fail one. | [test_github_status.py](../tests/test_github_status.py), [test_templates.py](../tests/test_templates.py) |
| Full suite | real PostgreSQL | Passes with `ISSUELENS_TEST_DATABASE_URL` set, none skipped. | [M3 setup](m3.md#setup) |

## Limits

- CI runs after a push, not before a commit. There is no pre-commit hook, and a failing run blocks nothing, because branch protection stays off while one person develops and pushes to `main`.
- The workflow was not checked by actionlint or a local Docker container; GitHub's runs are its check.
- CI pins Pyright to 1.1.411, but the README's local command runs whatever Pyright is installed.
- `.python-version` names only 3.13, so CI installs the newest 3.13 release while a local environment may keep an older one. The first run found a test that passed only on the older one.
- The pinned actions and the `postgres:14` image are updated by hand. Dependabot is not set up, and PostgreSQL 14 reached its end of life in November 2026.
- A function marked `noqa` is not checked at all, so it can grow without notice.
- Complexity and statement counts ignore nesting depth. `quality.py`'s `label_consistency_fields` nests five levels deep and still passes both limits.
- ruff's statement count differs slightly from pylint's, for example in nested functions, so counts near 50 can differ by a few.
- These paths still have no test: `_get`'s network errors, a timeout right after a model retry, and a model response without usage.
- The behavior recording wraps `AgentRun` methods by name, such as `_model_request`, `snapshot` and `_execute_tool`. A rename breaks the script, not the app.
- The frontend has no tests, so lint and types check its code but not its behavior. The brand link's reason comes from the Next.js documentation and was not checked in a browser.
- `react-hooks/set-state-in-effect` reports only the first call in an effect, so the marker on the mount effect also hides any later call in it.
- `eslint-config-next` brings its own rule list, unlike ruff's explicit `select`. The lockfile pins it, but a version update can add rules.
- The three plugins that do not list ESLint 10 could break the lint in a later update until they support it.
- ruff's broader default rule set reports 92 findings, 57 of them import order. It is not enabled.

## Tradeoffs and exercises

- **An explicit rule list versus the tool's defaults:** an explicit list changes only by decision. Defaults follow the tool's latest advice, but an upgrade can fail the checks overnight.
- **Marking existing violations versus fixing them first:** markers let the limits start now and keep the debt visible in the code. A marked function is unchecked until it is split.
- **Recording and differential testing versus unit tests:** the first two prove that nothing changed, including cases nobody thought of, but they also preserve old bugs. Unit tests state the rules that later changes must keep.
- **ruff versus pylint:** ruff is fast and holds every rule in one tool. pylint has more inference-based rules, which Pyright covers here.
- **ESLint 10 versus 9:** 9 installs without warnings but is no longer supported. 10 is current and supported by Next.js, at the cost of peer warnings until the plugins catch up.
- **PostgreSQL in CI versus skipping it:** the database checks add about 40 seconds locally, but M3's storage, checkpoints and Worker resume are checked on every push.
- **SHA pins versus version tags:** a SHA always runs the same code. A tag gets fixes automatically but can be moved to different code.
- **Following a lint rule versus marking it:** a rule states the usual case. The brand link and the mount effect are deliberate exceptions, and following the rule there would change what the page does.

Exercise: remove the marker on `jobs.py`'s `_run` and run `uv run --locked ruff check` to see its scores. Restore the marker, move the status decision after a finished run into its own function, and run ruff again. If `_run` drops to a complexity of 12 and 50 statements or fewer, RUF100 asks you to remove the marker.

Exercise: in `page.tsx`, replace the brand link's `<a>` with `<Link>` from `next/link` and remove its marker. Open an analysis, select the logo, and check whether the old result stays on screen while the address shows `/`.

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
- `9580db7`: Lint the frontend with ESLint and the backend's limits, marking `Home` and two deliberate patterns.
- CI: run every check in GitHub Actions, with the tests against PostgreSQL.
- Batch 2: split `snapshot` and `_execute_tool`, leaving 8 exceptions.

## Next step

Lint batch 3, `jobs.py`'s `_run`. It runs every MCP analysis, and with batch 4's `context.py` `build` it is one of the last two functions on that path over the limits.
