# Contributing

Thanks for looking. This is a small, deliberately scoped project — read
[AGENTS.md](AGENTS.md) first for conventions, accumulated gotchas, and the
decisions already settled.

## Setup

Uses [uv](https://docs.astral.sh/uv/). Not pip, not poetry.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # if you do not have it
git clone https://github.com/matthiasbigl/err2issue && cd err2issue
uv sync --group dev
uv run pytest -q          # should be green in ~6 seconds
```

No `.env` and no credentials are needed to run the tests — nothing in the suite
touches the network.

## The loop

```bash
uv run pytest -q                     # fast; run it constantly
uv run ruff check src tests --fix
uv run ruff format src tests         # CI enforces this
```

Before pushing:

```bash
uv run ruff check src tests && uv run ruff format --check src tests && uv run pytest -q
```

## Commits

**[Conventional Commits](https://www.conventionalcommits.org/), enforced by CI
on the pull request title** (it becomes the squash-merge subject).

```
<type>(<optional scope>): <subject>
```

| Type | Use for |
|---|---|
| `feat` | New capability |
| `fix` | Bug fix |
| `perf` | Performance, no behaviour change |
| `refactor` | Restructuring, no behaviour change |
| `test` | Tests only |
| `docs` | Documentation only |
| `build` | Dependencies, Dockerfile, packaging |
| `ci` | Workflows |
| `chore` | Everything else |
| `revert` | Reverts a previous commit |

Scope is the module: `fingerprint`, `routing`, `filer`, `otlp`, `redact`,
`suppress`, `ai`, `config`, `docker`, `integrations`.

Subject: imperative mood, lower case, no trailing period. Append `!` for a
breaking change and explain it in the body.

```
fix(fingerprint): normalize unit-suffixed durations

`\b\d{4,}\b` never matched "3000ms" because a trailing word boundary
fails against a unit suffix, so per-occurrence timeout values leaked
into the identity and split one bug across many issues.

Fixes #42
```

```
feat(fingerprint)!: switch to v2 normalization rules

BREAKING CHANGE: errors previously tracked under err2issue-fp-v1-*
are filed fresh under err2issue-fp-v2-*. Existing issues stay open
with their history; see docs/FINGERPRINT.md#versioning.
```

## Tests

**Tests are the specification.** `tests/test_fingerprint.py` is the executable
form of the fingerprint contract; `tests/test_filer.py` is the executable form of
the dedup guarantee.

Rules:

- **No network.** `respx` intercepts httpx. If you need a GitHub response, mock
  the endpoint.
- **No sleeping.** Time is injected — take a `clock` and advance it. A test that
  sleeps will be rejected; the suite must stay fast enough to run on every save.
- **Test the failure path.** What happens when GitHub is down, the payload is
  malformed, the config is wrong, the model refuses? Most bugs live there.
- **Name the behaviour, not the function.**
  `test_losing_the_label_race_records_an_occurrence_instead_of_duplicating`
  beats `test_file_2`.
- Coverage floor is **85%**, enforced in CI.

## Changing a contract

Two things in this repository are contracts with live installs. Both need care
beyond a normal change.

### The fingerprint

Changing normalization changes the identity of every error in every deployment.

**Do not edit the v1 rules.** Ship `v2`: bump `VERSION`, document the new rules
alongside the old ones in [docs/FINGERPRINT.md](docs/FINGERPRINT.md), add a v2
golden-vector test while keeping the v1 one, and note it in AGENTS.md.

### The issue format

Consumers parse the machine header and the `[xN]` title. Adding a section is
safe. Changing the header format or the title convention is breaking — update
[docs/ISSUE_CONTRACT.md](docs/ISSUE_CONTRACT.md) in the same pull request.

The pull request template has a checkbox for both. Tick it honestly.

## Diagrams

Architecture and flow changes need the diagram updated in the same pull request.
Mermaid by default — it renders on GitHub and diffs as text. For Excalidraw,
**commit the `.excalidraw` source** to `docs/diagrams/`; a picture with no source
cannot be edited by the next person.

## Pull requests

1. Branch from `main`.
2. Make the change, with tests.
3. `uv run ruff check src tests && uv run ruff format --check src tests && uv run pytest -q`
4. Open the PR. The title must be a valid Conventional Commit — CI checks it.
5. Fill in the template. **Paste real test output** rather than asserting that
   tests pass.

Small and focused beats large and comprehensive. If a change touches the
fingerprint, the issue format, and the Dockerfile, that is three pull requests.

## Reporting a bug

Include what you would want if you were fixing it: what you did, what happened,
what you expected, and the output of `GET /stats` if the service was running.
For a filing bug, the fingerprint (`v1:abc123…`) and the issue URL make it
reproducible in seconds.

## Security

Do not open a public issue for a vulnerability — particularly anything involving
redaction failing to mask a secret shape, since a public report would be a
worked example. Use GitHub's private vulnerability reporting.

## Scope

err2issue is a pipe, not a platform. Deliberate non-goals, from
[PLAN.md](PLAN.md) §3: no UI, no metrics pipeline, no query API, no automated
fixing.

Fix agents are *consumers* — they read the issue contract from outside. If you
want err2issue to fix bugs, the answer is
[integrations/](integrations/), not a new module here.
