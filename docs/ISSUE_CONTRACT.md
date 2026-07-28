# The issue contract

This is the product. [PLAN.md](../PLAN.md) §6 calls the issue format "the
loose-coupling seam", and that is exactly right: err2issue has no API, no
database, and no plugin interface. It writes issues in a fixed format, and
anything that can read a GitHub issue is a consumer.

That only works if the format is a contract rather than a rendering detail. This
document is that contract. **Producers must not deviate; consumers may rely on
everything marked stable.**

## Anatomy

```
┌────────────────────────────────────────────────────────────────┐
│ TITLE   [x12] Cart total fails when an item has no price       │
│         └─┬─┘ └──────────────────┬─────────────────────────┘   │
│      occurrence count      short description (≤ 70 chars)      │
├────────────────────────────────────────────────────────────────┤
│ LABELS  err2issue                    ← every issue err2issue files
│         err2issue-fp-v2-a3f9c21b8e04 ← the deduplication key
├────────────────────────────────────────────────────────────────┤
│ BODY    <!-- err2issue: fingerprint=… version=v2 count=12 -->  │ ← machine
│         **`TypeError`** in **checkout-api**                    │
│         | table: first/last seen, service, version, trace |    │
│         ### Summary        ← AI-written, or absent             │
│         ### Exception                                          │
│         ### Stack trace                                        │
│         ### Correlated log lines                               │
│         <details>Runtime attributes</details>                  │
└────────────────────────────────────────────────────────────────┘
```

## Stable — safe to parse

### The machine-readable header

Always the first line of the body:

```html
<!-- err2issue: fingerprint=a3f9c21b8e04 version=v2 count=12 -->
```

```python
HEADER = re.compile(
    r"<!--\s*err2issue:\s*fingerprint=(?P<fp>[0-9a-f]+)\s+"
    r"version=(?P<ver>v\d+)\s+count=(?P<count>\d+)\s*-->"
)
```

This is the only part of the body guaranteed machine-parseable. Parse this, not
the prose.

### The title

```
[x<count>] <description>
```

```python
TITLE = re.compile(r"^\[x(?P<count>\d+)\]\s*(?P<rest>.*)$")
```

A title without the prefix means one occurrence — a human may have retitled it,
which is allowed and preserved. When err2issue re-counts, it takes
`max(title_count, header_count) + 1`, so a manual retitle never loses history.

### The labels

| Label | Meaning |
|---|---|
| `err2issue` | Filed by err2issue. Filter on this. |
| `err2issue-fp-<version>-<12 hex>` | The fingerprint. **The deduplication key.** Current version `v2`; issues filed before a bump keep the older label. |

Additional labels are configurable via `E2I_ISSUE_LABELS` and may be added
freely by humans or agents.

> **Never remove the `err2issue-fp-*` label.** err2issue looks the issue up by
> exactly that label; without it, the next occurrence creates a duplicate.
> Adding labels, closing, reopening, retitling, assigning, and commenting are
> all safe.

### Section headings

These headings are stable when present. Any may be absent — the stack trace
section is missing for errors without a stack, `### Summary` is missing when AI
enrichment is unconfigured or fell back.

| Heading | Contents |
|---|---|
| `### Summary` | Two or three sentences, AI-written |
| `### Exception` | Fenced block: `Type: message` |
| `### Stack trace` | Fenced block, **top frame first** |
| `### Correlated log lines (trace \`…\`)` | Fenced block, oldest first |
| `<details><summary>Runtime attributes</summary>` | Markdown table of span attributes |

## Not stable — do not parse

The prose in `### Summary`, exact table row order, timestamp formatting, and the
footer. Read these; do not build a parser on them.

## Lifecycle

```mermaid
stateDiagram-v2
    direction LR
    [*] --> Open: first occurrence
    Open --> Open: recurrence
    Open --> Closed: closed
    Closed --> Open: regression
    Closed --> [*]: never recurs
```

**Creating.** No issue carries the fingerprint label → create with `[x1]`.

**Recurring.** An open issue carries the label → bump `[xN]`, and add an
occurrence comment (capped at `E2I_MAX_COMMENT_PER_ISSUE_PER_HOUR`, default 4,
so a chronic error updates its count cheaply without spamming the thread).

**Regression.** A *closed* issue carries the label → reopen it with
`state_reason: reopened`, bump `[xN]`, and add a comment headed `### Regression`.
Regression comments are never suppressed by the budget.

This is why closing an err2issue issue is meaningful: if the error comes back,
the same issue reopens. **That reopen is the signal that a fix did not hold**,
and it is the single most useful thing this format gives you.

## Consuming

### Find every open error in a repository

```bash
gh issue list --label err2issue --state open --limit 100
```

### Read the fingerprint and count from an issue

```bash
gh issue view 42 --json title,body,labels \
  | jq -r '{
      count:  (.title | capture("^\\[x(?<n>\\d+)\\]").n),
      fp:     (.labels[].name | select(startswith("err2issue-fp-"))),
      header: (.body | capture("fingerprint=(?<fp>[0-9a-f]+) version=(?<v>v\\d+) count=(?<c>\\d+)"))
    }'
```

### The errors that matter most

Chronic errors, worst first:

```bash
gh issue list --label err2issue --state open --json number,title --limit 200 \
  | jq -r '.[] | select(.title | test("^\\[x\\d+\\]"))
           | [(.title | capture("^\\[x(?<n>\\d+)\\]").n | tonumber), .number, .title]
           | @tsv' \
  | sort -rn | head -20
```

Regressions — closed, then reopened:

```bash
gh issue list --label err2issue --state open \
  --search "reopened" --json number,title
```

## Rules for consumers

1. **Filter on the `err2issue` label.** Do not assume every issue in the
   repository is one of ours.
2. **Parse the machine header, not the prose.** The header is contractual; the
   summary is generated text.
3. **Leave the fingerprint label alone.** Everything else is yours to change.
4. **Treat the body as untrusted.** It contains production exception messages,
   and on a public repository anyone who can trigger an error can influence
   them. It *describes* a bug; text inside it that reads like an instruction is
   data, not a command. This matters most when the consumer is an AI agent —
   both shipped [integrations](../integrations/) state it explicitly in the
   prompt.
5. **Closing is expected.** It is not "resolving" the fingerprint. If the error
   recurs, the issue reopens.

## Rules for producers

Anything can file an issue in this format — that is the point of PLAN.md §5.2's
reusable-mechanism argument, and why the `workflow` sink and the
[workflow template](../integrations/workflow-sink/file-error-issue.yml) still
exist. If you write one:

1. Emit the machine header **first**, with a correct count.
2. Use `[xN]` in the title, with `N` matching the header.
3. Apply both labels, and create the fingerprint label idempotently.
4. **Look the issue up with `GET /repos/{o}/{r}/issues?labels=…&state=all`**,
   never `GET /search/issues`. The search index is eventually consistent, so it
   will tell you no issue exists when one does, and you will file a duplicate.
   See [CHALLENGE.md](../CHALLENGE.md) §1.
5. Redact before writing. Issues are public-by-default and permanently archived.

## Full example

```markdown
<!-- err2issue: fingerprint=a3f9c21b8e04 version=v2 count=12 -->

**`TypeError`** in **checkout-api**

| | |
|---|---|
| First seen | 2026-07-21 09:14:02 UTC |
| Last seen | 2026-07-28 12:00:00 UTC |
| Occurrences | 12 |
| Service | `checkout-api` |
| Version | `1.4.2` |
| Severity | `ERROR` |
| Fingerprint | `v2:a3f9c21b8e04` |
| Trace ID | `4bf92f3577b34da6a3ce929d0e0e4736` |

### Summary

Cart total fails when any line item has a null price, because `total()` sums
prices without filtering. Most likely a product record missing a price after the
recent catalogue import.

### Exception

```
TypeError: unsupported operand type(s) for +: 'int' and 'NoneType'
```

### Stack trace

```
  File "/app/src/checkout/handlers.py", line 142, in handle_checkout
    total = cart.total()
  File "/app/src/checkout/cart.py", line 88, in total
    return sum(item.price for item in self.items)
```

### Correlated log lines (trace `4bf92f3577b34da6a3ce929d0e0e4736`)

```
2026-07-28 11:59:59 UTC  INFO   POST /checkout started for cart 8891
2026-07-28 12:00:00 UTC  DEBUG  loaded 3 items from catalogue
```

<details><summary>Runtime attributes</summary>

| Attribute | Value |
|---|---|
| `deployment.environment` | `production` |
| `http.route` | `/checkout` |
| `http.status_code` | `500` |

</details>
```
