# Deployment

err2issue is one stateless container. It needs a GitHub credential, a
destination, and error records pushed at it. Nothing else.

## 1. Collector configuration

This is the only change your telemetry stack needs, and it does not touch your
existing pipeline. Copy the marked sections from
[examples/otel-collector-config.yaml](../examples/otel-collector-config.yaml).

```yaml
processors:
  # Keep only errors. err2issue then sees a tiny fraction of your log volume.
  filter/errors_only:
    error_mode: ignore
    logs:
      log_record:
        - 'severity_number < SEVERITY_NUMBER_ERROR and attributes["exception.type"] == nil'

exporters:
  otlphttp/err2issue:
    endpoint: http://err2issue:4318
    encoding: json              # `proto` works too
    retry_on_failure: { enabled: true }
    sending_queue: { enabled: true, queue_size: 1000 }

service:
  pipelines:
    logs:                        # your existing pipeline — unchanged
      receivers: [otlp]
      exporters: [your-backend]

    logs/err2issue:              # a separate pipeline, so the filter cannot
      receivers: [otlp]          # affect what reaches your backend
      processors: [filter/errors_only, batch]
      exporters: [otlphttp/err2issue]
```

The exporter's retry queue is what makes err2issue safe to restart or lose: the
collector holds the errors and replays them.

**Applications need no changes.** They already export to the collector.

## 2. Authentication

### Personal access token — simplest

Fine-grained PAT with **Issues: read & write** on each target repository, or a
classic PAT with `repo`.

```bash
E2I_GITHUB_TOKEN=github_pat_...
```

Good for one or a few repositories. The rate limit is 5,000/hr for your whole
user account, shared with everything else using that token.

### GitHub App — recommended for organisations

One App installed on an org files into every repository it is granted. Its rate
limit **scales with the installation** — 5,000/hr floor, rising with repository
and user count to 12,500/hr — rather than being a fixed per-user budget. Tokens
are short-lived and minted per installation.

1. **Create it** at `https://github.com/settings/apps/new` (or under your org's
   settings for an org-owned App).
   - Uncheck **Webhook → Active**. err2issue does not receive webhooks.
   - Repository permissions: **Issues: Read & write**. Nothing else — this App
     cannot read your code.
2. **Generate a private key** and save the `.pem`.
3. **Note the App ID** from the settings page.
4. **Install it** on the org, granting the repositories that should receive
   issues.

```bash
E2I_GITHUB_APP_ID=123456
E2I_GITHUB_APP_PRIVATE_KEY_FILE=/run/secrets/err2issue-app.pem
```

err2issue resolves the installation per repository and caches the token,
refreshing before expiry. Pin it with `E2I_GITHUB_APP_INSTALLATION_ID` if you
prefer one installation.

> **Escaping the key in an env var.** Inline PEM contents need `\n` for
> newlines. Prefer `..._FILE` and mount the `.pem`; it avoids the whole problem.

### GitHub Enterprise

```bash
# GitHub Enterprise Server
E2I_GITHUB_API_URL=https://ghes.example.com/api/v3

# GHE.com data residency
E2I_GITHUB_API_URL=https://api.SUBDOMAIN.ghe.com
```

## 3. Routing

**Single repository:**

```bash
E2I_GITHUB_REPO=acme/backend
```

**Organisation — route by service:**

```bash
E2I_ROUTE_MAP=checkout-api=acme/checkout,cart-*=acme/cart,*-worker=acme/workers
E2I_GITHUB_REPO=acme/platform    # fallback for anything unmatched
```

Exact matches beat globs, and longer globs beat shorter ones, so `cart-api` wins
over `cart-*`. Verify the effective table:

```bash
curl -s localhost:4318/stats | jq .routing
```

An unroutable service is dropped and counted (`err2issue_unrouted_total`). Set
`E2I_DROP_UNROUTED=false` to make it loud instead.

## 4. Run it

### Docker Compose

```bash
cp .env.example .env    # fill in credentials + destination
docker compose up -d
```

### Docker

```bash
docker run -d --name err2issue -p 4318:4318 \
  -e E2I_GITHUB_TOKEN="$GITHUB_TOKEN" \
  -e E2I_GITHUB_REPO=acme/backend \
  -e E2I_ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  ghcr.io/matthiasbigl/err2issue:latest
```

### Kubernetes

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: err2issue
spec:
  replicas: 2
  selector:
    matchLabels: { app: err2issue }
  template:
    metadata:
      labels: { app: err2issue }
    spec:
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        seccompProfile: { type: RuntimeDefault }
      containers:
        - name: err2issue
          image: ghcr.io/matthiasbigl/err2issue:latest
          ports: [{ containerPort: 4318 }]
          env:
            - name: E2I_GITHUB_APP_ID
              valueFrom: { secretKeyRef: { name: err2issue, key: app-id } }
            - name: E2I_GITHUB_APP_PRIVATE_KEY_FILE
              value: /secrets/app.pem
            - name: E2I_ROUTE_MAP
              value: "cart-*=acme/cart,checkout-api=acme/checkout"
            - name: E2I_GITHUB_REPO
              value: acme/platform
          volumeMounts:
            - { name: app-key, mountPath: /secrets, readOnly: true }
          # Liveness restarts a wedged process. Readiness pulls a pod that
          # cannot reach GitHub out of rotation WITHOUT restarting it — pointing
          # liveness at /readyz would turn a GitHub outage into a crash loop.
          livenessProbe:
            httpGet: { path: /healthz, port: 4318 }
            periodSeconds: 30
          readinessProbe:
            httpGet: { path: /readyz, port: 4318 }
            periodSeconds: 10
          resources:
            requests: { cpu: 50m, memory: 128Mi }
            limits:   { memory: 256Mi }
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: { drop: ["ALL"] }
      volumes:
        - name: app-key
          secret: { secretName: err2issue, items: [{ key: private-key, path: app.pem }] }
```

**On replicas.** Suppression state is per-process, so N replicas can each file
the same error once. Dedup catches it — the second becomes an occurrence comment
rather than a duplicate issue — so the cost is extra API calls, not correctness.
Two replicas is a reasonable ceiling; err2issue is not throughput-bound.

## 5. Verify

```bash
curl -s localhost:4318/healthz            # {"status":"ok"}
curl -s localhost:4318/readyz | jq        # ready + any config problems
./examples/send-sample-error.sh           # end-to-end: files a real issue
curl -s localhost:4318/stats | jq
```

err2issue **refuses to start** on an unusable configuration and prints exactly
what is wrong. If the container exits immediately, read its logs — the answer is
there.

## Tuning noise

Defaults are conservative. Start there and loosen.

| Setting | Default | Raise it when | Lower it when |
|---|---|---|---|
| `E2I_SUPPRESS_WINDOW_SECONDS` | 600 | Chronic errors comment too often | You want faster recurrence signal |
| `E2I_MAX_DISPATCHES_PER_MINUTE` | 30 | Many services share one deployment | Approaching GitHub's 80/min content limit |
| `E2I_MAX_NEW_FINGERPRINTS_PER_DAY` | 50 | A large migration legitimately produces many new errors | A bad deploy flooded you once |
| `E2I_MAX_COMMENT_PER_ISSUE_PER_HOUR` | 4 | — | Threads are too noisy |

GitHub's secondary limits are **80 content-creating requests/minute and 500/hour**,
shared with the web UI. The defaults sit well under both.

## Monitoring

`/metrics` is Prometheus format:

| Metric | Watch for |
|---|---|
| `err2issue_error_events_total` | Baseline error volume |
| `err2issue_suppressed_total` | Healthy. A spike means a crash loop was contained. |
| `err2issue_unrouted_total` | **Non-zero means errors are being dropped** — a service has no destination |
| `err2issue_filed_total{action=…}` | `created` / `commented` / `reopened` / `skipped` |
| `err2issue_failed_total` | **Non-zero means filing is failing** — check credentials and permissions |
| `err2issue_dropped_backpressure_total` | Sustained non-zero means GitHub is too slow for your volume |

`/stats` is the same data plus routing and suppression state, for humans.

## Troubleshooting

**Container exits at startup.** Fail-fast working as intended. The log names the
problem — usually a missing credential or an unparseable `E2I_ROUTE_MAP`.

**No issues appear.** Walk the pipeline:

```bash
curl -s localhost:4318/stats | jq '.metrics'
```

- `received_records: 0` → the collector is not reaching err2issue. Check the
  exporter endpoint and the collector's own logs.
- `error_events: 0` → records arrive but none qualify. Your filter may be too
  aggressive, or the instrumentation is not setting `exception.type`.
- `unrouted > 0` → no destination for that `service.name`.
- `failed > 0` → the GitHub call is failing. Check the logs for the status code.

**Duplicate issues for one error.** Confirm the fingerprint label is still on
the original issue. Removing it is the usual cause — err2issue looks the issue
up by exactly that label.

**`Resource not accessible by integration`.** The App or PAT lacks
**Issues: write** on that repository, or the App is not installed on it.

**Issues are too noisy.** Raise `E2I_SUPPRESS_WINDOW_SECONDS`. If distinct
errors are being filed for what is really one bug, that is a fingerprinting
issue — see [FINGERPRINT.md](FINGERPRINT.md) and file a report with both
fingerprints.
