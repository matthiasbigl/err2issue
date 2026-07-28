# err2issue — Design Plan

Status: **accepted design, pre-implementation**
Date: 2026-07-28

---

## 1. Problem

Teams running OpenTelemetry have plenty of *telemetry* but no automatic, low-friction path from "a new error occurred in production" to "there is exactly one tracked, deduplicated, context-rich work item for it." Existing error trackers solve this by becoming a destination: their own database, UI, grouping engine, and paid AI features. Teams that already coordinate work in GitHub — including teams using GitHub-native automation and fix agents — don't want a second destination. They want a pipe.

## 2. Goals

- Every **unique** production error becomes **exactly one** GitHub issue.
- Re-occurrences are counted and visible at a glance (title prefix `[xN]`).
- Regressions (an error returning after its issue was closed) reopen the issue.
- Each issue carries a complete, documented **context package** sufficient for a human or an automated fix agent to act without access to the telemetry backend.
- **Zero owned state**: no database, no persistent store beyond GitHub itself.
- **Loose coupling**: the issue is the API. Anything that can create a well-formed dispatch can file issues; anything that can read issues can consume them.
- Generic from day one: works with any OTel Collector and any OpenAI-compatible LLM endpoint; configured entirely through environment variables and workflow inputs.

## 3. Non-goals (v1)

- No UI, no dashboard, no query API.
- No metrics pipeline, no alerting rules engine, no SLOs.
- No automated fixing — err2issue ends at the issue. Fix agents are consumers, not components.
- No multi-tenant SaaS concerns; self-hosted, single-repo scope.

## 4. Architecture

```
apps ──OTLP──▶ OTel Collector ──▶ existing backend (traces/logs/metrics — unchanged)
                    │
                    │ NEW (collector config only):
                    │   filter processor — error logs / exception records only
                    │   otlphttp exporter ─────────────┐
                    ▼                                  ▼
              existing backend              err2issue receiver (new, tiny)
                                                 │ 1. fingerprint
                                                 │ 2. storm suppression
                                                 │ 3. assemble context package
                                                 │ 4. POST workflow_dispatch
                                                 ▼
                                    .github/workflows/file-error-issue.yml
                                    (workflow_dispatch; reusable mechanism)
                                                 │ dedup via gh issue search
                                                 ▼
                                             GitHub Issue
```

### 4.1 Why push, not poll

The receiver is an OTLP/HTTP endpoint that the collector *pushes* to via a filtered exporter. This is:

- **Standard**: OTLP is the one protocol every OTel setup already speaks. Works with any collector distribution and with backends that relay OTLP.
- **Complete**: no polling window, no dependence on a particular backend's query API or retention. Errors are seen as they flow.
- **Doctrine-friendly**: adding an exporter is a collector-config-only change; applications never know err2issue exists.

### 4.2 Why GitHub is the only store

Dedup state is the set of issues carrying a `err2issue-fingerprint:<hash>` label. "Have we seen this error?" is an issue search. Re-occurrence is a comment plus a title recount. This eliminates the database, the migrations, and the backup story — and makes GitHub's search, notifications, permissions, and automation ecosystem free features of the system.

## 5. Components

### 5.1 Receiver service

A small service (single binary/container) with two endpoints:

- `POST /v1/logs` — OTLP/HTTP logs receiver (protobuf and JSON content types).
- `GET /healthz` — liveness/readiness.

Processing per log record with severity ≥ ERROR or an `exception.type` attribute:

1. **Fingerprint** — `sha256(service.name + exception.type + normalized-top-frame)[:12]`. Normalization strips line numbers, column numbers, and memory addresses from the top application frame so the same bug hashes identically across releases, while distinct bugs hash differently. (Exact normalization rules are a documented, versioned part of the fingerprint contract.)
2. **Storm suppression** — an in-memory cache ensures at most one dispatch per fingerprint per configurable window (default 10 min), plus a global dispatch rate cap. Crash loops cannot flood the Actions queue. This cache is *only* a throttle; authoritative dedup lives in the workflow.
3. **Context assembly** — builds the context package: exception type/message/stack, trace id, service version, runtime attributes, timestamp, and the last K log records sharing the same trace id (small in-memory ring buffer keyed by trace id).
4. **Dispatch** — calls `POST /repos/{owner}/{repo}/actions/workflows/{workflow}/dispatches` with the fingerprint and context package as inputs. Retries with backoff; on persistent failure, logs and drops. The receiver hangs off the collector's exporter, so receiver failure can never affect applications (the collector's own retry queue absorbs outages).

Configuration (env only): listen address, GitHub token + repository + workflow name + ref, suppression window, rate caps, ring-buffer size.

### 5.2 `file-error-issue.yml` — the reusable workflow

A `workflow_dispatch` workflow that users copy into their repository (or reference). It is the reusable issue-filing mechanism: **anything** — the receiver, a failing CI job, a security scan, a human with `gh workflow run` — can invoke it with the same inputs.

Inputs: `fingerprint` (required), `service`, `exception_type`, `occurred_at`, `context` (markdown), `dry_run`.

Logic:

1. `concurrency: err2issue-<fingerprint>` serializes concurrent filings of the same error — no double-creation races.
2. Search open *and* closed issues for the `err2issue-fingerprint:<hash>` label:
   - **Open issue found** → add an occurrence comment (last-seen timestamp, fresh trace excerpt), parse `[xN]` from the title, retitle `[xN+1] …`.
   - **Closed issue found** → reopen it, add a `regression` comment with the new context, retitle.
   - **No issue** → generate title + summary (see 5.3), create the issue with labels `err2issue` and `err2issue-fingerprint:<hash>` (labels created idempotently).
3. `dry_run` prints every action it *would* take without writing — this is also the primary test harness.

Permissions: `issues: write` only, via the automatic `GITHUB_TOKEN`.

### 5.3 AI title & summary

On issue creation only, the workflow calls an **OpenAI-compatible chat-completions endpoint** (any provider or self-hosted gateway; key + base URL + model name configured via repository secrets/variables) to produce:

- a short title (≤ 70 chars), used as `[x1] <title>`;
- a 2–3 sentence summary for the issue body.

If the endpoint is unreachable or unconfigured, the workflow falls back to a deterministic title (`<exception.type>: <first line of message>`). The AI step is an enhancement, never a dependency.

## 6. The issue contract (the loose-coupling seam)

The issue format is fixed and documented. Consumers (triage scripts, fix agents, reporting) may rely on it; producers must not deviate.

- **Title**: `[x<count>] <short title>`
- **Labels**: `err2issue`, `err2issue-fingerprint:<hash>`
- **Body**:
  - machine-readable header: `<!-- err2issue: fingerprint=<hash> count=<N> -->`
  - first seen / last seen / occurrence count
  - service + version (+ commit link when resolvable)
  - exception type, message, top frames
  - trace id and sampled correlated log lines
  - runtime/environment attributes

## 7. Noise control & failure modes

| Risk | Mitigation |
|---|---|
| Crash loop floods Actions | per-fingerprint suppression window + global dispatch cap in receiver |
| New-error burst (bad deploy) | max new fingerprints per day, enforced in the workflow; breach → one summary issue instead of N |
| Concurrent filings of same error | workflow `concurrency` group per fingerprint |
| Receiver down | collector exporter retry queue absorbs; worst case some filings are lost — apps unaffected |
| GitHub API down | receiver retries with backoff, then drops and logs |
| AI endpoint down | deterministic fallback title |
| Sensitive data in stacks/messages | receiver truncates frames and messages to fixed limits; producer-side redaction remains the instrumentation's responsibility (unchanged from today) |
| Leaked receiver token | fine-grained PAT scoped to `actions: write` on a single repository — it cannot read code or touch issues |

## 8. Testing strategy

- **Unit tests** (receiver): fingerprint stability across line-number/address noise; distinctness of distinct bugs; suppression window; rate caps; context-package shape.
- **Workflow testing**: `dry_run` input exercised via `gh workflow run`; fixtures for open/closed/absent dedup paths; no live-AI tests (fallback path covered, AI path tested with a stubbed endpoint).
- **End-to-end smoke**: script posts a sample OTLP error payload to a local receiver and verifies the dispatch.

## 9. Deliverables

1. Receiver service (source + container image).
2. `file-error-issue.yml` workflow template + setup docs.
3. Collector configuration snippet (filter + exporter) for adopters.
4. Sample-payload script for local development and demos.

## 10. Alternatives considered

- **Adopting an existing error tracker (Sentry class)**: rejected — introduces a destination (DB + UI), its GitHub integration produces thin link-back issues that an agent can't act on without API access to the tracker, and its AI-fix features are a separate paid product. The value here is the pipe, not another platform.
- **Polling a telemetry backend's query API from a cron job**: rejected — couples to one backend's (often unofficial) API and retention window; misses errors between polls. Push over OTLP is standard and complete.
- **Agent-facing task trackers with GitHub sync (e.g. beads)**: not needed for filing — but explicitly noted as a compatible *consumer-side* work queue. Because GitHub issues are the contract, such tools can sync the issues err2issue creates and offer claim/dependency semantics to fix agents, with zero changes to this design.
- **Direct application export to the receiver**: rejected — the collector must remain the single fan-out point; applications stay unaware of err2issue.

## 11. Open questions (pre-implementation)

- Exact frame-normalization rules per language ecosystem (start language-agnostic; refine with fixtures).
- Whether the workflow should also be published as a reusable `workflow_call` workflow in addition to the copy-in template.
- Binary/container naming and registry publishing strategy.
