#!/usr/bin/env bash
# Post a realistic OTLP error payload straight at err2issue.
#
#   ./examples/send-sample-error.sh                    # default endpoint
#   ./examples/send-sample-error.sh http://host:4318   # explicit endpoint
#
# Run it twice: the first call files an issue, the second is absorbed by the
# suppression window. Wait out E2I_SUPPRESS_WINDOW_SECONDS (or restart the
# service) and the third call becomes an occurrence comment with `[x2]`.
set -euo pipefail

ENDPOINT="${1:-http://localhost:4318}"
NOW_NANOS="$(( $(date +%s) * 1000000000 ))"

read -r -d '' STACK <<'EOF' || true
Traceback (most recent call last):
  File "/app/src/checkout/handlers.py", line 142, in handle_checkout
    total = cart.total()
  File "/app/src/checkout/cart.py", line 88, in total
    return sum(item.price for item in self.items)
TypeError: unsupported operand type(s) for +: 'int' and 'NoneType'
EOF

# jq builds the payload so the stack trace is escaped correctly.
PAYLOAD="$(jq -n --arg stack "$STACK" --arg ts "$NOW_NANOS" '
{
  resourceLogs: [{
    resource: { attributes: [
      { key: "service.name",        value: { stringValue: "checkout-api" } },
      { key: "service.version",     value: { stringValue: "1.4.2" } },
      { key: "deployment.environment", value: { stringValue: "production" } }
    ]},
    scopeLogs: [{
      logRecords: [
        {
          timeUnixNano: $ts,
          severityNumber: 9,
          severityText: "INFO",
          traceId: "4bf92f3577b34da6a3ce929d0e0e4736",
          body: { stringValue: "POST /checkout started for cart 8891" }
        },
        {
          timeUnixNano: $ts,
          severityNumber: 17,
          severityText: "ERROR",
          traceId: "4bf92f3577b34da6a3ce929d0e0e4736",
          spanId: "00f067aa0ba902b7",
          body: { stringValue: "checkout failed" },
          attributes: [
            { key: "exception.type",       value: { stringValue: "TypeError" } },
            { key: "exception.message",    value: { stringValue: "unsupported operand type(s) for +: '"'"'int'"'"' and '"'"'NoneType'"'"'" } },
            { key: "exception.stacktrace", value: { stringValue: $stack } },
            { key: "http.route",           value: { stringValue: "/checkout" } },
            { key: "http.status_code",     value: { stringValue: "500" } }
          ]
        }
      ]
    }]
  }]
}')"

echo "POST ${ENDPOINT}/v1/logs"
curl -sS -X POST "${ENDPOINT}/v1/logs" \
  -H 'Content-Type: application/json' \
  -d "$PAYLOAD" \
  -w '\nHTTP %{http_code}\n'

echo
echo "Pipeline state:"
curl -sS "${ENDPOINT}/stats" | jq '{filed: .metrics.filed, suppressed: .metrics.suppressed, queue_depth}'
