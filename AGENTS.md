# AGENTS.md

Working notes for anyone — human or agent — changing this repository.

This file stays short on purpose. The detail lives one link away:

| Read when | |
|---|---|
| You are about to touch anything | [docs/GOTCHAS.md](docs/GOTCHAS.md) — traps that have already cost someone time, each paired with what to do instead |
| Changing identity rules | [docs/FINGERPRINT.md](docs/FINGERPRINT.md) — a versioned contract |
| Changing the issue body or title | [docs/ISSUE_CONTRACT.md](docs/ISSUE_CONTRACT.md) — a versioned contract |
| Adding or editing a diagram | [docs/diagrams/README.md](docs/diagrams/README.md) |
| Wondering why, not what | [CHALLENGE.md](CHALLENGE.md) — the design review that changed the architecture before implementation |

**Both files are living logs.** Learn something non-obvious → add it to
GOTCHAS.md. Settle a question that should stay settled → add it to
[Decisions](#decisions-and-why) below. Prune whatever stopped being able to
cause a mistake.

**The `AGENTS.md` files under `integrations/*/` are not this file.** They are
runbooks for an agent setting up a *consumer* in somebody else's repository.
When you are changing err2issue itself, this file applies wherever in the tree
you are working.

---

## What this project is

err2issue turns OpenTelemetry error records into deduplicated GitHub issues.
One unique error → exactly one issue, with an occurrence count, regression
reopening, and enough context to act on without opening a telemetry backend.

## Commands

```bash
uv sync --group dev                       # setup (uv, not pip)
uv run pytest -q                          # full suite, ~6s, no network
uv run pytest tests/test_filer.py -q      # one file
uv run ruff check src tests scripts --fix
uv run ruff format src tests scripts      # CI enforces this
uv run pytest --cov=err2issue --cov-report=term-missing
uv run python scripts/check_docs.py       # links + Mermaid conventions

docker build -t err2issue:local .
E2I_SINK=dry-run E2I_GITHUB_REPO=acme/api uv run err2issue
```

Everything CI will run on your branch, in one line:

```bash
uv run ruff check src tests scripts && uv run ruff format --check src tests scripts \
  && uv run pytest -q && uv run python scripts/check_docs.py
```

Line length is 100 (`ruff`, configured in `pyproject.toml`). CI additionally
renders every Mermaid diagram; see [Diagrams](#diagrams).

## Layout

```
src/err2issue/
  app.py           FastAPI: /v1/logs, /healthz, /readyz, /metrics, /stats
  pipeline.py      orchestration + metrics — read this to understand the flow
  otlp.py          OTLP decode (protobuf + JSON), error selection
  fingerprint.py   VERSIONED identity contract — see docs/FINGERPRINT.md
  redact.py        secret masking, runs before anything reaches GitHub
  suppress.py      per-fingerprint window, rate cap, daily new-error budget
  routing.py       service.name -> owner/repo
  context.py       issue body/comment builders + trace ring buffer
  ai.py            Anthropic title/summary, always falls back
  sinks.py         github (default) | workflow | dry-run
  config.py        env-only settings + fail-fast validation
  github/
    auth.py        PAT and GitHub App installation tokens
    client.py      REST client, retries, rate-limit handling
    filer.py       dedup + create/comment/reopen + the label mutex
scripts/
  check_docs.py    link + Mermaid checks, run by CI
```

`pipeline.py` and `github/filer.py` are where the interesting behaviour lives.

## Principles

**Fail fast.** A misconfigured deployment refuses to start rather than accepting
telemetry it will silently drop. `Settings.validation_errors()` checks
everything checkable without the network — route map parses, App key signs,
custom regexes compile, limits are positive. Add new checks there, not at first
use. The one deliberate exception is AI enrichment, which degrades gracefully by
design (PLAN.md §5.3: never a dependency).

**err2issue must never affect the telemetry path.** Filing happens off the
request path; a full queue sheds and reports it via OTLP `partial_success`; a
sink failure is caught, counted, and dropped. If you add something that can
raise into `/v1/logs`, you have broken this.

**Redact before anything else.** Before fingerprinting (a secret must not change
an error's identity) and before enrichment (a secret must not be sent to a
model).

**Two contracts are load-bearing.** The fingerprint and the issue format. Both
are versioned and documented. Changing either affects live installs.

## Diagrams

**Every architectural or flow explanation gets a diagram.** Mermaid by default;
Excalidraw only for hand-drawn sketches, with the `.excalidraw` source committed
to `docs/diagrams/`. Never add a diagram without committing what generated it.

**Parsing is not rendering, and rendering is not readable.** `check_docs.py`
catches labels that render wrong, CI renders every block to catch syntax errors,
and neither can see that a layout came out a mess — so render the picture and
look at it. Conventions and the one-liner for doing that:
[docs/diagrams/README.md](docs/diagrams/README.md).

## Commits and pull requests

**Conventional Commits, enforced by CI on the PR title.**

```
<type>(<optional scope>): <subject>

fix(fingerprint): normalize unit-suffixed durations
feat(routing): support regex service patterns
docs: explain the label mutex
feat(fingerprint)!: switch to v2 rules      # ! marks a breaking change
```

Types: `feat`, `fix`, `perf`, `refactor`, `test`, `docs`, `build`, `ci`,
`chore`, `revert`. Imperative mood, lower case, no trailing period.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow.

## Testing

Tests are the specification, particularly `test_fingerprint.py` — it is the
executable form of the fingerprint contract.

- **No network, ever.** `respx` intercepts httpx.
- **No sleeping.** Time is injected (`FakeClock`), so windows and rate limits
  are tested by advancing a fake clock. The suite runs in about six seconds and
  must stay that way.
- **Test the failure, not just the success.** Every module has tests for what
  happens when the dependency is down, the input is malformed, or the config is
  wrong.
- Coverage floor is 85%, enforced in CI. Currently ~92%.

---

## Gotchas

**[docs/GOTCHAS.md](docs/GOTCHAS.md) — read it before you touch the matching
area.** Every entry is something that has already cost someone time here, and
every one of them looks reasonable until it bites.

| Area | The kind of thing in there |
|---|---|
| GitHub API | Why dedup cannot use the search endpoint; why a 422 on `POST /labels` is success; why a plain 403 must not be retried |
| OTLP | Collectors gzip by default; the filter processor drops what matches; `log_conditions:` does not exist in any released build |
| Redaction | Replace spans, not text — the obvious one-liner leaks the password |
| Fingerprinting | Why `\b\d{4,}\b` misses `3000ms`; why Python's error site is the *last* frame |
| Anthropic API | A refusal is HTTP 200; `effort` and `format` do not go in the same `output_config` |
| Docs and diagrams | The Mermaid traps `check_docs.py` enforces |
| Everything else | Secret-scanning fixtures, `TestClient` lifespan, `pull_request:` trigger types |

## What to do without asking, and what not to

Safe on your own: run the suite, run `ruff --fix`, run `check_docs.py`, render
diagrams, and run the service against the **`dry-run` sink**, which needs no
credentials and writes nothing. Reproduce every report that way first.

Stop and ask a human before:

- **Filing into a real repository.** The `github` sink creates issues, comments,
  and labels that someone then has to clean up. `E2I_SINK=dry-run` shows you the
  same decisions with none of the consequences.
- **Changing `VERSION` in `fingerprint.py`, or the issue header or title
  format.** Both re-identify every error in every live install. The procedure is
  in the contract docs; the decision is not yours to make alone.
- **Clicking a secret-scanning unblock URL.** It whitelists that string for the
  whole repository. Build the fixture from concatenated fragments instead — see
  GOTCHAS.md.

Never commit a credential. `.env.example` documents where secrets live and what
they are called; it contains no real ones, and it stays that way.

---

## Decisions and why

Recorded so nobody re-opens them without new information.

| Decision | Why | Where |
|---|---|---|
| File via REST, not `workflow_dispatch` | 204-no-body means no feedback; needs a file on every repo's default branch; input caps strain large stack traces | [CHALLENGE.md §2](CHALLENGE.md) |
| Label creation as the mutex | Atomic create-or-422 arbitrated by GitHub; no coordination service, no owned state | [CHALLENGE.md §4](CHALLENGE.md) |
| Fingerprint version in the label | A rules change otherwise orphans every existing issue silently | [docs/FINGERPRINT.md](docs/FINGERPRINT.md) |
| Redaction on by default | The destination is public-by-default and permanently archived — categorically different from a telemetry backend | [CHALLENGE.md §7](CHALLENGE.md) |
| Filing is off the request path | Three GitHub round-trips plus an LLM call would exceed the collector's export timeout and trigger retries | `app.py` |
| Two path segments in normalization | One merges `app/handler.py` with `worker/handler.py`; the full path splits the same bug per build machine | [docs/FINGERPRINT.md](docs/FINGERPRINT.md) |
| AI is never a dependency | Preserved verbatim from PLAN.md §5.3; every failure path returns the deterministic title | `ai.py` |
| `/healthz` and `/readyz` are separate | A container that cannot reach GitHub should leave the load balancer, not restart-loop | `app.py` |

---

## Open questions

Unresolved. Pick one up if you are looking for work.

- **Multi-replica suppression.** The window is per-process, so N replicas can
  each file the same error once. Dedup catches it (the second becomes a
  comment), so the cost is API calls rather than duplicate issues — but a shared
  throttle would be cheaper. Any fix must not reintroduce owned state.
- **The label-mutex race is narrowed, not closed.** A process paused between
  creating the label and creating the issue can still produce a duplicate. The
  window is one HTTP round-trip. Worth measuring before engineering further.
- **A real gh-aw run is unverified.** The workflow now compiles clean on gh-aw
  v0.83.4 and CI keeps it that way, but nobody has watched it actually fix a bug
  and open a pull request. Compiling proves the configuration is valid, not that
  the prompt works. Someone with a repository receiving real err2issue issues
  should run it and report back.
- **Per-language frame normalization.** Currently one language-agnostic rule
  set. PLAN.md §11 left this open and it is still open; refine with fixtures,
  and ship any change as `v3` — v2 is released.
