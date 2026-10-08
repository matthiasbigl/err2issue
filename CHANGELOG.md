# Changelog

All notable changes to err2issue. Versions follow [Semantic Versioning](https://semver.org/);
while the major version is 0, a minor release may change behaviour, and every such change is
listed under **Behaviour changes**.

## Unreleased

### Security

- **The runtime image is now distroless** (`gcr.io/distroless/cc-debian13`), replacing
  `python:3.12-slim-bookworm`. The slim base carried 55 HIGH/CRITICAL CVEs with no fix
  available, including CRITICALs in zlib and SQLite, in packages the service never runs:
  util-linux, ncurses, perl, systemd libraries, and the dependencies vendored into the base
  image's pip. CI passes `--ignore-unfixed`, so it stayed green, but a deployment scanner
  without that flag rejected the image. The new runtime holds glibc, libssl, CA
  certificates, uv's standalone CPython and the venv. It has no shell, no package manager
  and no pip.
- **Python comes from uv's standalone build, not Debian's.** Debian's `python3.13`
  (3.13.5) lags upstream by eleven patch releases and carries five unfixed HIGH CVEs. The
  standalone build tracks upstream releases: 3.13.16 with OpenSSL 3.5.9 at release.

### Behaviour changes

- Python in the image is **3.13**, up from 3.12. CI already tests 3.13.
- **The image has no shell.** `docker exec … sh` no longer works; use
  `docker exec <c> python -c …` for debugging. The process still runs as uid/gid 10001, so
  `runAsUser: 10001` manifests are unaffected.

## v0.5.1 — 2026-09-26

### Security

- **cryptography 49.0.0 → 50.0.1** (CVE-2026-69247, HIGH: a Bleichenbacher oracle in PKCS#7
  EnvelopedData decryption). err2issue reaches `cryptography` only through PyJWT, to sign
  GitHub App tokens, and never decrypts PKCS#7, so the vulnerable code was not reachable; the
  image carried it all the same, and image scanners rightly block on it.
- CI now scans the built image with trivy (fixable HIGH/CRITICAL fail the build) before it
  is published, so a vulnerable dependency stops here instead of in a deployment's scan.

No behaviour changes. Drop-in: pull the new image.

## v0.5.0 — 2026-09-26

v0.5 is about the issue itself: making sure what err2issue files says what actually went
wrong, stays accurate as the error recurs, and is safe to publish.

**No re-identification.** The fingerprint is still `v2` and the issue header and `[xN]`
title format are unchanged. Every existing issue keeps deduplicating exactly as before; new
occurrences land on the same issues.

### Highlights

- **The log message is kept.** A record from `logger.exception("… in shielded future")`
  carries the useful sentence in its body and a bare `str(exc)` — often `None` — in
  `exception.message`. err2issue used to title that issue `ConnectionClosedError: None` and
  drop the body. It now shows the body as a `### Log message` section, puts it in occurrence
  comments, gives it to the AI, and uses it for the title when the exception message is empty
  or a placeholder (`None`, `null`, the type name). That issue is now titled
  `ConnectionClosedError exception in shielded future`.
- **AI summaries actually appear.** The model's summary was discarded and the title repeated
  under `### Summary`. The summary is now used; when AI is off or falls back, the section is
  omitted, as the issue contract always said.
- **Issue bodies stay current.** On every recurrence the machine header's `count=`, the
  *Last seen* and *Occurrences* rows are updated in place (they were frozen at the first
  occurrence), and a *Latest version* row appears when a new service version starts failing.
  Human edits elsewhere in the body are preserved.
- **At-a-glance rows.** When the telemetry carries them: *Environment*, *Host* (pod and
  namespace), *Location* (`file:line in function`), *Request* (`GET /route → 500`),
  *Logger*, and *Escaped* (unhandled). Old and current semantic-convention names both work.
- **Trace links.** Set `E2I_TRACE_URL_TEMPLATE` and every trace id links to your tracing
  backend.
- **Long stack traces keep their tail.** Truncation used to keep only the head, which cut
  exactly the frame that matters for Python (last frame) and Java (`Caused by:`).

### Fixed

- Correlated log lines were written to issues **without redaction**. They now pass through
  the same redactor as the error.
- `/v1/logs` returned **500 on valid-but-unexpected JSON** (wrong field types, enum names
  for `severityNumber`, out-of-range timestamps), so the collector retried the batch forever.
  Malformed input is now skipped or answered with 400, never 500.
- Errors that set **only `severity_text`** (`ERROR`, `FATAL`, `CRITICAL`, … with
  `severity_number` 0) or **only `exception.message`** were missed. Both are now selected.
- The **all-zero trace id** several SDKs send outside a span was treated as one shared trace,
  so unrelated log lines appeared as "correlated" on every span-less error. It is now ignored.
  Uppercase and base64 trace ids are normalised to lowercase hex.
- Issue and comment bodies over GitHub's 65,536-character limit were rejected with 422 and
  the error was never filed. Bodies are now clipped (header intact, fences closed) with a
  truncation note.
- Exception text containing ```` ``` ````, `|` or newlines could break out of code blocks and
  tables. Fences now outgrow their content and table cells are escaped.
- A GitHub App token revoked mid-life caused 401s until expiry; a 401 now refreshes it once.
- Secondary rate limits without `retry-after` waited on the *primary* reset time (up to an
  hour, or not at all). They now wait GitHub's documented 60 seconds.
- A refused occurrence comment (locked or deleted issue) reported the whole filing as failed
  even though the count had been updated.

### Security and privacy

- Redaction now masks credentials in URL query strings (`X-Amz-Signature`, `token`,
  `access_token`, `api_key`, `client_secret`, …) keeping the parameter name, and masks the
  whole value of attributes whose *key* names a credential (`db.password`,
  `http.request.header.cookie`).
- The AI prompt wraps production text in a `<telemetry>` block and instructs the model to
  treat it as data, not instructions.
- AI summaries are sanitised before they are posted: `@mentions` and issue references
  cannot notify anyone or cross-link, links show their target, HTML is escaped.
- The log message in occurrence comments is fenced, so it cannot `@mention` either.

### Cost and operations

- AI enrichment is cached per fingerprint; a recurring error no longer costs a model call
  every suppression window. Fallback results are not cached, so they retry.
- New metrics: `err2issue_rejected_requests_total{reason}` (a collector with the wrong
  content type or encoding used to look like "no errors"), and
  `err2issue_suppressed_by_reason_total{reason="window|rate|budget"}`.
- When the daily new-error budget runs out, err2issue logs a warning once per day and
  `/stats` reports `new_dropped_today`. It used to drop errors with only a debug log.
- err2issue's labels are created with a colour and description instead of grey and blank.
- Multiple issues carrying one fingerprint label are logged as a warning naming all of them.

### New settings

| Variable | Default | Purpose |
|---|---|---|
| `E2I_TRACE_URL_TEMPLATE` | *(empty)* | Link trace ids, e.g. `https://grafana.example.com/explore?traceId={trace_id}`. Validated at startup. |
| `E2I_REOPEN_NOT_PLANNED` | `false` | Reopen issues a human closed as *not planned* or *duplicate* when the error recurs. |
| `E2I_ISSUE_ASSIGNEES` | *(empty)* | Comma-separated logins assigned to new issues (max 10). Falls back to unassigned if GitHub rejects them. |

### Behaviour changes

Review these before upgrading:

- **Issues closed as *not planned* or *duplicate* are no longer reopened.** Their count
  still rises and they still receive (budgeted) occurrence comments, but they stay closed.
  Issues closed as *completed* reopen as before. Set `E2I_REOPEN_NOT_PLANNED=true` for the
  old behaviour.
- **More records become errors**: `severity_number` 0 with an error-level `severity_text`,
  and records carrying only `exception.message`. Expect some new issues if your SDKs emit
  these.
- **Issue bodies are edited on every recurrence** (header count, last-seen, occurrences,
  latest version). Consumers parsing the header now see the real count.
- **New issues' titles** use the log message when the exception message is uninformative.
  Existing issues keep their titles.
- **Fences may be longer than three backticks** when content contains backticks. Parse
  fences as CommonMark does; the machine header is unaffected.
- **Refused occurrence comments** are reported as `commented` with a note, not as failures.

### Upgrading

Drop-in: pull the new image and restart. No migration, no re-labelling. Optionally set
`E2I_TRACE_URL_TEMPLATE`, and decide whether you want `E2I_REOPEN_NOT_PLANNED=true`.

### Still open

- Stackless errors whose exception message is a placeholder (`None`) still share one
  fingerprint per type, even when their log messages differ. Separating them changes
  identity and would ship as fingerprint `v3`.
- Structured (map) log bodies still render as `{key=value, …}`; extracting a `message` key
  would also change fingerprints for severity-only errors.
