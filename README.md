# err2issue

**OpenTelemetry errors in, deduplicated GitHub issues out.**

err2issue watches your OpenTelemetry pipeline for errors and turns each *unique* error into exactly one GitHub issue — with an AI-written title, an occurrence counter in the title (`[x12] …`), and all the context (stack, trace id, version) an engineer *or an automated fix agent* needs to act on it.

No database. No dashboard. No new destination to run. **GitHub is the store, the UI, and the notification system.**

This repository currently contains the **design plan** — see [PLAN.md](PLAN.md). Implementation follows.

## The idea in one picture

```
your apps ──OTLP──▶ OTel Collector ──▶ your existing backend (unchanged)
                         │
                         │ filtered fan-out: error logs only
                         ▼
                   err2issue receiver  (tiny OTLP/HTTP service)
                         │ fingerprints errors, suppresses storms
                         │ dispatches to GitHub
                         ▼
              reusable GitHub Actions workflow
                         │ dedup via issue search
                         │ comment + recount, reopen, or create
                         ▼
              GitHub Issue: "[x12] NullReference in profile-service"
```

## Why not an error tracker?

Classic error trackers are *destinations*: a database, a UI, a query language, and — increasingly — an expensive AI add-on to actually fix anything. If your team already lives in GitHub (and your automation does too), the tracker is overhead. err2issue is a **pipe**: it classifies errors into issues and gets out of the way. Occurrence counts, regression reopening, and an agent-ready context format are built in.

## Status

Design phase. Feedback welcome via issues.

## License

To be determined before the first code lands.
