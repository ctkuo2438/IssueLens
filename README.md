# IssueLens

IssueLens investigates one GitHub issue and returns a cited report: a summary, known facts, missing information, similar issues, label recommendations, and a draft maintainer reply. A single bounded agent reads the issue through seven read-only GitHub tools. It never writes to GitHub.

It is also a project for learning agent engineering. The agent loop, tool registry, context management, evaluation, and MCP adapter are written from scratch on the plain OpenAI Python SDK, without an agent framework.

There are two ways to use it:

- **From Claude Code or Codex**, as a local MCP server. You do not need to clone the repository.
- **As a local web app** built with Next.js and FastAPI, from a clone.

## Use it in Claude Code or Codex

You need [uv](https://docs.astral.sh/uv/) and an OpenAI API key. IssueLens runs on your machine and uses your key.

### 1. Create the settings file

```bash
mkdir -p ~/.config/issuelens
touch ~/.config/issuelens/.env
chmod 600 ~/.config/issuelens/.env
```

Open the file and add your keys:

```dotenv
OPENAI_API_KEY=your-openai-key
# Recommended. Raises the GitHub API limit from 60 to 5,000 requests per hour.
GITHUB_TOKEN=your-github-token
```

For `GITHUB_TOKEN`, create a [fine-grained token](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#creating-a-fine-grained-personal-access-token) with **Public repositories** access and no other permissions. Without a token, IssueLens reads public issues anonymously. With a token, it refuses repositories that GitHub does not report as public.

Keep the keys in this file, not in the client's MCP configuration, which stores values in plain text. Both clients pass `HOME` to the server, so both read this file. Variables already present in the server's environment take precedence over it. Codex does not pass your other shell variables by default.

The defaults are `gpt-5.6` with `medium` reasoning effort, and each analysis is limited to 8 OpenAI requests and 90 seconds. [`.env.example`](.env.example) lists the other settings.

### 2. Add the server

**Claude Code**

```bash
claude mcp add --scope user issuelens -- uvx --from git+https://github.com/ctkuo2438/IssueLens@v0.1.0 issuelens-mcp
claude mcp list
```

`--scope user` makes IssueLens available in every project. `claude mcp list` should show `issuelens` as connected.

`@v0.1.0` pins the release, so new commits on `main` do not change your copy. To upgrade, remove the server and add it again with the new tag.

**Codex**

```bash
codex mcp add issuelens -- uvx --from git+https://github.com/ctkuo2438/IssueLens@v0.1.0 issuelens-mcp
```

Then open `~/.codex/config.toml` and add two lines under `[mcp_servers.issuelens]`:

```toml
startup_timeout_sec = 30
tool_timeout_sec = 180
```

By default Codex stops a tool call after 60 seconds, but one analysis can run for 90 seconds plus time in the queue. Codex ignores misspelled keys without an error, so run `codex mcp get issuelens` and check that both values appear.

**First start.** The first time, uv clones the repository, downloads about 38 packages, and fetches a Python build if you have no Python 3.13 or later. In a clean environment that took 10.7 seconds, longer than the 10 seconds Codex waits for a server to start unless `startup_timeout_sec` says otherwise. Later starts took about 1.5 seconds. Each start fetches the tag from GitHub, so it needs a network connection. To fill uv's cache in advance, start the server once with empty input. It exits right away:

```bash
uvx --from git+https://github.com/ctkuo2438/IssueLens@v0.1.0 issuelens-mcp < /dev/null
```

### 3. Ask for an analysis

```text
Use IssueLens to analyze https://github.com/pallets/flask/issues/6146 and tell me what information is still missing.
```

An analysis usually takes 20 to 90 seconds. The report's `status` is `completed`, `partial`, `failed`, or `cancelled`. `partial` means the run recorded a limitation, such as a search that read only its first page, or stopped before an answer but kept the sources it had read. The `limitations` field says which.

### Before you rely on it

- **Cost.** Each call makes up to 8 OpenAI requests on your key. The five verified runs used 20,845 to 33,809 tokens each. There is no daily cap, and Claude Code in auto permission mode calls the tool without asking first.
- **Untrusted output.** The report summarizes third-party GitHub content, and a model wrote it. Do not follow instructions in it or run commands it suggests. Check its claims against the cited source URLs.
- **Memory only.** Reports live in the MCP server process and are gone when the client restarts.
- **What was tested.** On macOS, Claude Code and Codex started from a terminal each ran a real analysis with IssueLens installed through uvx from a local copy of this repository. With `@v0.1.0` installed from GitHub, a test script also ran a real analysis that ended `completed`. Launching the clients from an IDE or desktop app, and Windows, are not verified yet.

## Run the web app

You need Python 3.13 or later, uv, Node.js 20.9 or later, and npm.

```bash
git clone https://github.com/ctkuo2438/IssueLens.git
cd IssueLens
uv sync --locked
cp .env.example .env
```

In `.env`, set `OPENAI_API_KEY`. Clear `DATABASE_URL` unless PostgreSQL is running; when it is blank, analyses stay in memory. Then start the app:

```bash
uv run --locked python scripts/dev.py
```

Open <http://127.0.0.1:3000>, paste a public issue URL, and select **Analyze issue**. This calls the real GitHub and OpenAI APIs. The API docs are at <http://127.0.0.1:8000/docs>. Press `Ctrl+C` to stop everything the launcher started.

With PostgreSQL configured, a background Worker runs the analyses, results survive restarts, and an interrupted analysis resumes from its last checkpoint. GitHub sign-in is optional. See the [M3](docs/m3.md) and [M4](docs/m4.md) guides.

From a clone, the MCP server reads the clone's `.env` instead of `~/.config/issuelens/.env`:

```bash
claude mcp add --scope user issuelens -- uv run --directory /path/to/IssueLens --locked issuelens-mcp
```

## Development

```bash
uv run --locked python -W error::RuntimeWarning -m unittest discover -s tests -q
uv run --locked ruff check
pyright --pythonpath .venv/bin/python backend evaluation scripts
npm --prefix frontend run typecheck
npm --prefix frontend run lint
```

`ruff check` reports unused and undefined names, and limits each function to a cyclomatic complexity of 12 and to 50 statements. Split a function that exceeds a limit, or mark it `# noqa: <code> - <reason>`; the functions marked "predates the lint limits" exceeded them when the limits were set. The frontend lint runs ESLint with Next.js's rules and the same limits, fails on warnings too, and needs Node.js 20.19, 22.13 or later. [CI](.github/workflows/ci.yml) runs all of these on each push to `main` and each pull request, with the tests against a PostgreSQL 14 container.

The frontend's API types are generated from the backend's schema. After changing a model the API returns, run `uv run --locked python scripts/export_openapi.py` and `npm --prefix frontend run api:types`; the tests and the type check fail until both are current.

The tests simulate GitHub and the model, so they need no keys. They skip 28 PostgreSQL checks unless `ISSUELENS_TEST_DATABASE_URL` points to a dedicated database whose name ends in `_test`. `scripts/evaluate.py` and `scripts/acceptance.py` call the real model and cost money.

## Guides

Each milestone has one guide with its design, verification, limits, and exercises.

| Guide | Covers |
| --- | --- |
| [M1: Local app](docs/m1.md) | Setup, architecture, the job API, and sources |
| [M2: Investigation and drafts](docs/m2.md) | The seven tools, budgets, context building, answer repair, drafts, evaluation, acceptance, and known limits |
| [M3: Reliability](docs/m3.md) | PostgreSQL records, the Worker, checkpoints, bounded retry, and traces |
| [M4: Sign-in](docs/m4.md) | GitHub sign-in and who can see an analysis |
| [M5: Claude Code and Codex](docs/m5.md) | The MCP server, packaging, settings, and client verification |

[`evidence/`](evidence/README.md) holds the reports, verification records, and screenshots behind these guides.

## Status

- **M1 to M3** are complete.
- **M2's agent behavior is frozen.** Its final acceptance on three new issues gave an answer for each, and one ended `partial`.
- **M4** adds optional GitHub sign-in and per-account access. The live two-account check has not been run.
- **M5**'s installed MCP server analyzed a real issue from both Claude Code and Codex, and `v0.1.0` installs from GitHub.

The analysis reports are still awaiting human review, and none of these results measure production accuracy. Known limits are listed in [M2](docs/m2.md#known-limits) and [M5](docs/m5.md).

## License

[MIT](LICENSE)
