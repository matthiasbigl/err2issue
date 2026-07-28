# Challenging PLAN.md

Status: **review of the accepted design, written before implementation**
Date: 2026-07-28

The problem statement in [PLAN.md](PLAN.md) §1 is right, and the core insight — *the issue is the API, GitHub is the store* — is worth building. This document records where the design as written would not have delivered on its own goals, and what the implementation does instead. Everything below is a disagreement with the plan, not with the idea.

---

## 1. The dedup mechanism is not correct

**Plan §5.2:** *"Search open *and* closed issues for the `err2issue-fingerprint:<hash>` label"* — implemented via `gh issue search`.

`gh issue search` hits `GET /search/issues`. That is an **index**, not the issues table. Three consequences, each of which breaks the plan's own §2 goal ("exactly one GitHub issue"):

| Property | Value | Why it breaks this design |
|---|---|---|
| Consistency | Eventually consistent | An issue created seconds ago is often **not yet searchable**. A crash loop that gets two dispatches past suppression creates **two issues** for one fingerprint. |
| Rate limit | **30 requests/minute**, separate pool | A bad deploy producing 60 new fingerprints per minute stalls the pipeline. |
| Result cap | 1,000 results, `incomplete_results` flag | Silent partial results — the dedup query can return "not found" for an issue that exists. |

The `incomplete_results` field exists precisely because this endpoint is allowed to lie to you. Building the system's only source of truth on it is the single most serious defect in the plan.

**What this implementation does instead:** dedup reads the primary datastore.

```
GET /repos/{owner}/{repo}/issues?labels=err2issue-fp-v1-<hash>&state=all
```

This is a direct table read: strongly consistent, no index lag, no 1,000-result cap, and it draws from the ordinary 5,000/hr core pool instead of the 30/min search pool. The lookup is exact rather than a text query, so there is no relevance ranking to reason about.

Cost of the change: labels must be created before they can be filtered on, and one label is created per unique error. That is a real cost, and it buys a correctness property the plan otherwise does not have. It also turns out to be load-bearing for §4 below.

## 2. The `workflow_dispatch` hop is the wrong seam

**Plan §4/§5.2:** receiver → `POST /actions/workflows/{wf}/dispatches` → a workflow the user copies into their repo → issue.

The plan justifies this as "the reusable issue-filing mechanism" (§5.2), and that is a genuine benefit — anything that can call the endpoint can file an issue. But as the *only* path it costs more than it returns:

- **`workflow_dispatch` returns `204 No Content`.** No run ID, no issue number, no outcome. The receiver cannot tell success from silent failure, cannot log the issue it filed, and cannot expose a meaningful metric. The plan's §7 row "GitHub API down → receiver retries with backoff, then drops and logs" is describing a system that cannot observe its own primary output.
- **The workflow file must exist on the repository's default branch.** For an org with 40 services, adoption means 40 pull requests before a single error can be filed — and 40 more for every change to the filing logic. This directly contradicts the "tiny, low-friction pipe" framing.
- **`workflow_dispatch` accepts at most 10 inputs.** The plan's §5.1 step 3 assembles a full context package (stack, trace ID, correlated log lines, runtime attributes) and §5.2 passes it as a single `context` markdown input. Large stack traces plus correlated logs will hit input size limits, and the failure is a rejected dispatch, not a truncated issue.
- **Two credentials instead of one.** A PAT with `actions: write` for the receiver, *plus* whatever the workflow uses. The plan's §7 argues the `actions: write` PAT "cannot read code or touch issues" — true, and a good property — but it is mitigating a risk the architecture introduced.
- **Actions minutes and latency per error**, for work that is three REST calls.
- **The AI step lands inside the workflow** (§5.3), so the LLM key becomes a secret in *every adopting repository*.

**What this implementation does instead:** the service files the issue directly. `workflow_dispatch` is kept as an **optional output mode** (`E2I_SINK=workflow`) for teams who want the Actions seam, and the reusable workflow ships as a template for the "anything can file an issue" use case. The seam the plan actually wants — *loose coupling* — is preserved by the **issue contract** (§6 of the plan), not by the transport. Consumers read issues; they do not care that a workflow did or did not run.

## 3. Single-repo scope is a non-goal that the product needs

**Plan §3:** *"No multi-tenant SaaS concerns; self-hosted, single-repo scope."*

Fair as a v1 boundary, but it is the wrong boundary. A telemetry pipeline is inherently multi-service: one OTel Collector fans out errors from every service an org runs. Pinning the receiver to one repo means one deployment per repo, each with its own PAT, each duplicating the suppression state — and the storm-suppression cache then protects each repo individually rather than the Actions queue as a whole.

**What this implementation does instead:**

- **Routing**: `service.name` → `owner/repo`, via exact map, glob/regex rules, and a default repo, all env-configured. One deployment serves an entire org.
- **GitHub App authentication** alongside PATs. An App installed on an org mints short-lived installation tokens for every repo it is granted, with a rate limit that *scales with the installation* (5,000/hr floor, up to 12,500/hr) rather than a fixed per-user PAT budget. This is the difference between "works for my repo" and "works for an org", and it is why the plan's §11 open questions should have included auth.
- **GitHub Enterprise Server and GHE.com data residency** via a configurable API base URL. The plan assumes github.com throughout.

## 4. Concurrent filing has no answer once the workflow is gone

**Plan §5.2 step 1** relies on Actions `concurrency: err2issue-<fingerprint>` to serialize double-filing. Drop the workflow and that mutex disappears — and even with the workflow it only serializes within one repository's Actions queue, not across two receiver replicas dispatching simultaneously.

**What this implementation does instead:** use label creation as a distributed mutex. `POST /repos/{o}/{r}/labels` is atomic — it returns `201` to exactly one caller and `422 already_exists` to everyone else, arbitrated by GitHub's database. So:

1. Try to create `err2issue-fp-v1-<hash>`.
2. `201` → this process owns creation of the issue for this fingerprint. Create it.
3. `422` → someone else created the label. Re-query the issues list (consistent, per §1) and treat the result as an occurrence rather than a new issue.

This gives single-issue-per-fingerprint across arbitrarily many replicas with no coordination service and no owned state — which is the property the plan wanted and the mechanism it was missing. It only works because dedup reads the consistent endpoint; on the search index the re-query in step 3 would return nothing and file a duplicate anyway.

## 5. The fingerprint contract is unversioned

**Plan §5.1:** `sha256(service.name + exception.type + normalized-top-frame)[:12]`, and §11 leaves normalization rules open.

The rules *will* change — §11 says so explicitly ("refine with fixtures"). When they do, every existing error re-fingerprints and re-files as a brand-new issue, and every previously-filed issue becomes permanently unreachable. The plan calls the fingerprint "a documented, versioned part of the contract" but puts no version anywhere in the artifact.

**What this implementation does instead:** the version is in the label itself — `err2issue-fp-v1-<hash>`. A rules change ships as `v2`, old issues stay addressable under `v1`, and the two can coexist during a migration. The normalization rules are pinned in [docs/FINGERPRINT.md](docs/FINGERPRINT.md) with the fixtures that lock them.

Two smaller notes on the same mechanism:
- **Label names cap at 50 characters.** `err2issue-fp-v1-` (16) + 12 hex = 28. Fits, with room for `v10`. Worth stating, because a longer prefix would silently break filtering.
- **Errors with no stack trace exist** (logged errors, `panic` in some runtimes, JS errors crossing a bundler boundary). The plan's fingerprint has no defined behaviour when the top frame is absent. Here it falls back to a normalized message digest, and that fallback is part of the versioned contract.

## 6. "Zero owned state" is not quite true, and the gap matters

**Plan §2:** *"Zero owned state: no database, no persistent store beyond GitHub itself."* **Plan §5.1** then specifies an in-memory suppression cache and an in-memory ring buffer keyed by trace ID.

That is state. It is not *durable* state, which is the property actually being claimed, and the distinction has a consequence the plan does not draw out: **on restart, suppression is lost, so a fingerprint already filed can dispatch again immediately.** The plan is aware of this ("This cache is *only* a throttle; authoritative dedup lives in the workflow") — but that is exactly why the authoritative dedup has to be correct. A lossy throttle in front of a lagging search index produces duplicate issues on every deploy of the receiver.

With consistent dedup (§1) plus the label mutex (§4), a lost suppression window costs one extra API round-trip and an occurrence comment. That is the right failure mode, and it is only available because of the other two changes.

## 7. Secrets in stack traces are treated as someone else's problem

**Plan §7:** *"producer-side redaction remains the instrumentation's responsibility (unchanged from today)."*

This is the one place I would push back on the plan's risk table rather than its architecture. Everywhere else in a telemetry stack, an unredacted secret lands in a backend behind SSO with retention limits. Here it lands in a **GitHub issue** — indexed, notified to watchers, emailed, and on a public repository, permanently public and archived by third parties. The blast radius is categorically different, so inheriting the upstream posture unchanged is not a neutral choice.

Truncation (which the plan does specify) limits volume, not exposure: an API key in an exception *message* is in the first 200 characters.

**What this implementation does instead:** a redaction pass runs before anything is written to GitHub, covering common token shapes (GitHub `gh[pousr]_`, AWS `AKIA`, Slack `xox[baprs]-`, Bearer/Authorization headers, JWTs, connection-string passwords, `PRIVATE KEY` blocks, and generic `key|secret|token|password=...` assignments). It is on by default, extensible with custom patterns, and can be disabled. It is defence in depth, not a substitute for producer-side hygiene — the plan is right that instrumentation should not emit secrets. It is just not right that this component should assume it never does.

## 8. Smaller gaps

| Gap | Handling here |
|---|---|
| §5.1 `/healthz` conflates liveness and readiness | Split `/healthz` (process alive) and `/readyz` (GitHub auth resolved, routing valid) — a container that cannot reach GitHub should fail readiness, not restart-loop |
| No behaviour defined when the label exists but the issue was deleted | Falls through to create, and re-uses the orphaned label |
| No behaviour for archived repos / repos with Issues disabled | Detected, logged once per repo, and dropped rather than retried forever |
| No observability of the pipeline itself | `/metrics` exposes received / filtered / suppressed / filed / deduped / failed counters |
| §8 "no live-AI tests" but AI is on the create path | AI is behind an interface with a deterministic fallback; both branches are unit-tested with a stubbed client, and the fallback is what runs when unconfigured |
| Severity mapping unstated | OTLP `severity_number >= 17` (ERROR) per the OTel spec, **or** presence of `exception.type` — stated and tested |

---

## What the plan got right, and this keeps

These are not concessions; they are the reasons the project is worth building.

- **GitHub as the only store.** Dedup state as a label set is a genuinely good idea. The change in §1 is about *which endpoint* reads that state, not about the model.
- **Push over poll, via OTLP.** Plan §4.1 is correct on every point: standard, complete, and collector-config-only. Kept exactly.
- **The issue contract as the coupling seam.** Plan §6 is the real product. Fixed, documented, machine-readable header included — see [docs/ISSUE_CONTRACT.md](docs/ISSUE_CONTRACT.md).
- **AI as an enhancement, never a dependency.** Plan §5.3's fallback rule is the right instinct and is preserved verbatim.
- **Occurrence counting and regression reopening.** The `[xN]` title convention and closed-issue reopen are what make this better than a webhook that spams issues.
- **Rejecting the error-tracker-as-destination model.** Plan §10's reasoning holds up.

## Summary of changes

| # | Plan says | Implementation does | Severity |
|---|---|---|---|
| 1 | Dedup via issue **search** | Dedup via **issues list filtered by label** | **Correctness** |
| 2 | Receiver → `workflow_dispatch` → workflow → issue | Service files directly; workflow is an optional sink + template | Architecture |
| 3 | Single repo, PAT only | `service.name` routing, GitHub App auth, GHES/GHE.com support | Scope |
| 4 | Actions `concurrency` mutex | Label creation as distributed mutex | **Correctness** |
| 5 | Unversioned fingerprint | Version in the label: `err2issue-fp-v1-<hash>` | Migration |
| 6 | "Zero owned state" | Same design, stated honestly; safe because of #1 and #4 | Clarity |
| 7 | Redaction is upstream's job | Redaction on by default before write | **Security** |
| 8 | Assorted undefined edges | Defined and tested | Robustness |
