# Gotchas

Things that have already cost someone time in this repository. Each entry is
a trap plus the thing to do instead.

**This file is a living log.** When you hit something non-obvious, add it
here. When a note stops being able to cause a mistake, delete it. Both are
cheap, and this is the highest-value thing you can leave behind.

Referenced from [AGENTS.md](../AGENTS.md), which is the file to read first.

---

### GitHub API

- **`GET /search/issues` is not usable for dedup.** Eventually consistent,
  30 req/min, 1,000-result cap, and it sets `incomplete_results` because it is
  *allowed to return partial results*. It reports "no issue" for one created
  seconds ago. Always `GET /repos/{o}/{r}/issues?labels=…&state=all`.
  ([CHALLENGE.md §1](../CHALLENGE.md))
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
  migration: [FINGERPRINT.md](FINGERPRINT.md#v2).
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
- **A `---` divider needs a blank line above it.** Without one, Markdown reads
  it as setext syntax and turns the paragraph above into an `<h2>`, which also
  puts a phantom entry in the document outline. Nothing about the diff looks
  wrong.
- **GitHub's heading anchors do not collapse hyphens.** `github-slugger` strips
  punctuation and then replaces spaces one at a time, so `## Closing the loop:
  issue → fix` is `#closing-the-loop-issue--fix` — two hyphens, because two
  characters vanished from between two spaces.
- `scripts/check_docs.py` enforces all of the above, plus that every relative
  link resolves. CI then renders each block with `mermaid-cli`.

### GitHub Agentic Workflows (gh-aw)

Only relevant when changing `integrations/gh-aw/`. All of these were confirmed
against gh-aw v0.83.4 by compiling, not by reading documentation.

- **`gh aw compile` is a validator, so use it as one.** Unknown frontmatter
  keys, wrong value types, and correct keys at the wrong nesting level all fail
  the build — `roles:` at the top level is an error, `on.roles:` is fine. CI
  compiles the shipped workflow on every run. Probe a question with the compiler
  rather than reasoning about the schema.
- **Its warnings are run-time errors in disguise.** `toolsets: [default]`
  includes the `pull_requests` toolset, which needs `pull-requests: read`.
  Without it the workflow compiles with a *warning* and then fails when someone
  runs it. The CI gate treats any warning as a failure.
- **Grepping that build log for "warning" fails every build.** The success line
  is `0 error(s), 0 warning(s)` — it contains both words. Assert that exact
  summary instead. This gate shipped broken for exactly one commit.
- **A pull request opened with `GITHUB_TOKEN` does not start CI.** GitHub blocks
  it to prevent workflow recursion, so an agent's PR arrives with no checks —
  the PR you least want unverified. The fix is a `GH_AW_CI_TRIGGER_TOKEN` secret
  (fine-grained PAT, Contents: Read & Write) which gh-aw picks up by name and
  uses to push one empty commit.
- **"No PAT needed" is about inference only.** Since June 2026,
  `permissions: copilot-requests: write` bills Copilot through the built-in
  token — but only where the org has "Allow use of Copilot CLI billed to the
  organization" enabled. Elsewhere it silently falls back to needing a
  `COPILOT_GITHUB_TOKEN` PAT. Two different tokens, two different problems.
- **A GitHub App cannot authenticate Copilot inference.** Apps cover repository
  access. Model access is `copilot-requests: write` or a PAT, and nothing else.
- **Network ecosystem identifiers are a closed set.** `npm`, `pip` and `cargo`
  are compile errors; the identifiers are `node`, `python`, `rust`, and so on.

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
