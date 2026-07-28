# AGENTS.md

Working notes for anyone — human or agent — changing this repository.

**This file is a living log.** When you learn something non-obvious, hit a trap,
or make a decision the code cannot explain by itself, add it to
[Gotchas](#gotchas) or [Decisions](#decisions-and-why). It is the highest-value
thing you can leave behind, and it is cheap.

Keep it pruned. If a note no longer causes anyone to make a mistake, delete it.

**Other `AGENTS.md` files in this repository are not this file.** The ones under
`integrations/*/` are runbooks for an agent setting up a *consumer* in somebody
else's repository — they say nothing about changing err2issue. If you are
editing code here, this file is the one that applies, wherever in the tree you
are working.

---

## What this project is

err2issue turns OpenTelemetry error records into deduplicated GitHub issues.
One unique error → exactly one issue, with an occurrence count, regression
reopening, and enough context to act on without opening a telemetry backend.

Read in this order if you are new:

1. [README.md](README.md) — what it does
2. [CHALLENGE.md](CHALLENGE.md) — **why it does it that way**; the design review
   that changed the architecture before implementation
3. [docs/ISSUE_CONTRACT.md](docs/ISSUE_CONTRACT.md) — the output format
4. [docs/FINGERPRINT.md](docs/FINGERPRINT.md) — the identity rules

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

**Every architectural or flow explanation gets a diagram.** Prose describing a
pipeline is worse than a picture of it.

- **Mermaid** for anything in markdown — it renders natively on GitHub, diffs as
  text, and needs no tooling. This is the default; use it unless you have a
  reason not to.
- **Excalidraw** for hand-drawn conceptual diagrams where the sketch quality
  helps. **Commit the `.excalidraw` source to `docs/diagrams/`** so it stays
  editable — a PNG with no source is a dead end. Markdown cannot render
  `.excalidraw`, so export an SVG next to the source *if* you embed it, and not
  otherwise.

`docs/diagrams/` holds the sources and the full conventions:
[docs/diagrams/README.md](docs/diagrams/README.md). Do not add a diagram to a
doc without committing whatever generated it.

**Parsing is not rendering.** `scripts/check_docs.py` catches the mistakes that
make a diagram unreadable rather than invalid, and CI renders every block with
`mermaid-cli` because a syntax error ships as a red error box on the front page.
Neither can tell you that a layout is a mess — look at the picture before you
push one.

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

Things that have already cost someone time.

### GitHub API

- **`GET /search/issues` is not usable for dedup.** Eventually consistent,
  30 req/min, 1,000-result cap, and it sets `incomplete_results` because it is
  *allowed to return partial results*. It reports "no issue" for one created
  seconds ago. Always `GET /repos/{o}/{r}/issues?labels=…&state=all`.
  ([CHALLENGE.md §1](CHALLENGE.md))
- **Issues endpoints return pull requests too.** "Every pull request is an
  issue." Filter anything carrying a `pull_request` key or you will match a PR
  as if it were the error's issue.
- **`POST /labels` returning 422 is success, not failure.** It is the losing
  side of the creation mutex. `create_label()` returns a bool for this reason.
- **Label names cap at 50 characters.** `err2issue-fp-v2-<12 hex>` is 28. Do not
  lengthen the prefix without checking.
- **A 410 marks a repo unavailable, and that mark must expire.** Issues-disabled
  is a setting someone turns back on; remembering it until restart is silent
  data loss on a Ready pod. `IssueFiler` re-probes on a cooldown — do not
  "simplify" it back to a permanent set.
- **A plain 403 is not a rate limit.** Only treat 403 as retryable when
  `x-ratelimit-remaining: 0` or the body mentions a rate limit; otherwise it is
  a permissions failure and retrying just burns quota.
- Secondary rate limits: **80 content-creating requests/minute, 500/hour**,
  shared with the web UI.
- `workflow_dispatch` returns **204 with no body** — no run id, no issue number.
  Anything downstream of it is unobservable.
- **Push protection blocks realistic secret fixtures.** A test token has to look
  exactly like the real thing to be worth testing, which is precisely what the
  scanner rejects — `GH013`, push declined, on a file that contains no real
  credential. Build every fixture in `tests/test_redact.py` from concatenated
  fragments (`"xoxb-" + "1" * 12 + …`): the scanner reads the file text, not the
  evaluated Python. Do not resolve this by clicking the "allow this secret"
  unblock URL — that whitelists the string repository-wide. `AKIAIOSFODNN7EXAMPLE`
  is safe as a literal; it is AWS's published documentation key.

### OTLP

- **The collector gzips request bodies by default.** `otlphttp` enables
  compression unless told otherwise, and ASGI servers do not decompress request
  bodies — so a receiver that ignores `Content-Encoding` 400s on *every* export
  from a default-configured collector. This shipped broken and was only caught
  by running a real collector against the service; the unit tests all passed.
  Handled in `app.py:_decompress` (gzip, deflate, raw deflate, identity) with a
  64 MiB ceiling against decompression bombs.
- **`await request.body()` has no ceiling**, so bounding only the decompressed
  payload leaves the easier attack unbounded. Read through `app.py:_read_body`,
  which streams to the same cap; `Content-Length` alone is not enough, a chunked
  request declares no length.
- **Collectors send protobuf *or* JSON** depending on the exporter's `encoding`.
  Supporting one is a silent deployment trap: the receiver 415s and the
  collector retries forever.
- **The filter processor DROPS what matches**, it does not keep it. The
  condition describes what to throw away. Getting this backwards silently
  forwards everything *except* errors.
- **`log_conditions:` does not exist in any released contrib build.** Upstream
  docs on `main` describe it, but 0.140.0 rejects it with
  `has invalid keys: log_conditions`. Use `logs: { log_record: [...] }`.
  Validate with
  `docker run --rm -v $PWD/cfg.yaml:/c.yaml otel/opentelemetry-collector-contrib:0.140.0 validate --config=/c.yaml`.
- **An errors-only filter means no correlated log lines, ever.** err2issue's
  trace ring buffer can only correlate records it receives, so filtering to
  errors upstream leaves that issue section permanently empty (`context_lines`
  stays 0 on `/stats`). Forwarding INFO-and-above fixes it at a real volume
  cost. Both configurations are documented in the collector config and both
  have been verified end-to-end.
- The **JSON mapping permits both camelCase and snake_case** field names. Both
  are handled in `otlp.py`; do not "simplify" that away.
- Severity: **17–20 is ERROR, 21–24 is FATAL**, so `>= 17` covers both.
- Errors with **no stack trace are normal** (Go, JS across a bundler boundary,
  severity-only records). The fingerprint has a documented message fallback.

### Redaction

- **Replace spans, not text.** A rule that keeps part of its match needs a
  capture-group template (`_TEMPLATES` in `redact.py`), never
  `m.group(0).replace(m.group(1), MASK, 1)` — that masks the first occurrence of
  the *string*, which in `postgres:postgres@host` is the username, leaking the
  password. Equal user and password is the common case, not the edge one.

### Fingerprinting

- **`\b\d{4,}\b` does not match `3000ms`.** A trailing word boundary fails
  against unit suffixes, so per-occurrence durations leaked into the identity
  and split one bug into an issue per timeout value. Fixed by using `\d{3,}`
  unanchored. The test suite caught this.
- **Go prints the function line above the file line**, so the function line is
  what gets selected. That is deliberate — the file line also carries a `+0x1a`
  offset that changes on every recompile.
- **Python prints its traceback outermost-first**, alone among the ecosystems
  here, so its error site is the *last* `File "` line. Only `File "` lines are
  eligible for that scan — a source excerpt like `    total()` matches the
  Go/C++ symbol pattern and would otherwise win. Rationale and the v1→v2
  migration: [docs/FINGERPRINT.md](docs/FINGERPRINT.md#v2).
- **Never edit a released version's rules in place.** Ship the next `vN`. An
  in-place edit re-fingerprints every error in every deployment at once, with
  no signal.

### Anthropic API

- **Do not put `effort` and `format` in the same `output_config`.** Each is
  documented on its own; the combination is not, and an unrecognised shape
  would 400 on every call — which fails *silently* here, because enrichment
  falls back to the deterministic title by design. `ai.py` sends only `format`.
- A refusal is **HTTP 200 with `stop_reason: "refusal"`**, not an exception.
  Check `stop_reason` before reading `content`.

### Documentation and diagrams

- **A valid Mermaid diagram can still be unreadable.** `stateDiagram-v2` places
  transition labels with no collision detection, so anything longer than two or
  three words overlaps the neighbouring label and the nodes themselves — the
  issue lifecycle diagram shipped like that. Keep labels short and put the
  detail in the prose underneath. This is invisible in review: the diff looks
  fine, only the rendered picture is wrong.
- **Backticks are not Markdown inside a Mermaid label.** `` `state_reason` ``
  renders as literal backtick characters. `<br/>`, `<i>` and `<b>` are the only
  markup that survives, and they work with or without `htmlLabels`.
- **Point an edge at a node, not at a subgraph.** `X --> mySubgraph` attaches to
  the container's boundary wherever the layout engine feels like, which reads as
  an arrow from nowhere. Name the first node inside instead.
- **Label both arms of a decision.** One labelled arm and one bare arm reads as
  though the bare one is the default, whichever way round it is.
- `scripts/check_docs.py` enforces all four, plus that every relative link and
  `#heading` anchor resolves. CI then renders each block with `mermaid-cli`.

### Everything else

- **`ruff format` is enforced in CI.** Run it before pushing; it reflows a
  surprising amount on first contact.
- The `dry-run` sink is the escape hatch for running with no credentials at all
  — use it in examples and when reproducing a report.
- `TestClient` needs the context-manager form (`with TestClient(app)`) or the
  lifespan never runs and `app.state.service` does not exist.
- `pyproject.toml` uses `license = "Apache-2.0"` as a string. The
  `{ text = ... }` table form is deprecated and fails the build on modern
  setuptools.
- **A bare `pull_request:` trigger does not fire on `edited`.** The default set
  is `opened, synchronize, reopened`, so a PR-title check never re-runs when the
  title changes: a title fixed after review stays red, and a valid one edited
  into garbage stays green. `ci.yml` lists the types explicitly and guards the
  three expensive jobs with `if: github.event.action != 'edited'` so a
  description edit does not rebuild the image. Note `github.event.action` is
  empty on `push` and `workflow_dispatch`, so the guard is a no-op there.

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
- **`gh aw compile` is unverified.** The gh-aw workflow's frontmatter parses and
  uses documented fields only, but the extension could not be installed in the
  environment where it was written. Someone should compile it and report back.
- **Per-language frame normalization.** Currently one language-agnostic rule
  set. PLAN.md §11 left this open and it is still open; refine with fixtures,
  and ship any change as `v2`.
