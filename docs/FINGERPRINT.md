# The fingerprint contract

The fingerprint decides what "the same bug" means. Everything else in err2issue
follows from it: dedup, the occurrence count, regression detection, and whether
an engineer sees one issue or four hundred.

**Current version: `v1`.** These rules are frozen. Changing them changes the
identity of every error in the world, so a change ships as `v2` — see
[Versioning](#versioning).

## The digest

```
sha256( service.name  ␟  exception.type  ␟  site )  →  first 12 hex characters
```

`␟` is US (0x1F), a separator that cannot occur in any of the three fields.

Twelve hex characters is 48 bits. At ten thousand distinct errors in one
repository — far beyond what any real deployment reaches — the birthday
collision probability is about 1 in 6 million. A collision merges two bugs into
one issue, which is visible and recoverable; the digest is short because it also
has to fit in a GitHub label.

### `site` — where the error happened

If the event has a stack trace:

```
site = "frame:" + normalize_frame(top_frame(stacktrace))
```

Otherwise (a logged error with no stack — common in Go, in JS across a bundler
boundary, and for severity-only records):

```
site = "message:" + normalize_message(exception.message or body)
```

The `frame:` / `message:` prefix means the two modes can never collide.

## Selecting the top frame

Scan the stack from the top, skip preamble lines, and take the first line that
looks like a genuine frame.

**Skipped as preamble** (case-insensitive prefix match): `Traceback (most recent
call last)`, `Stack trace`, `Exception in thread`, `Caused by`, `The above
exception`, `During handling of`, `goroutine N`, `... N more`.

**Recognised as a frame** — any line matching one of:

| Pattern | Ecosystem |
|---|---|
| `^\s*File\s+"` | Python |
| `^\s*at\s+\S` | Java, JavaScript, .NET |
| `^\s*from\s+\S+:\d+` | Ruby |
| `:\d+:in\s` | Ruby |
| `^\s*#\d+\s+` | PHP, gdb |
| `\.go:\d+` | Go |
| `^\s*\S+\([^)]*\)\s*$` | Go function line, C++ symbol |

If no line matches, the first non-preamble line is used.

> **Go note.** Go prints the function line (`main.handler(0x1, 0x2)`) *above*
> the file line. The function line wins, deliberately: it carries the symbol,
> its pointer arguments normalize to `0xADDR`, and the file line additionally
> carries a `+0x1a` offset that shifts on every recompile.

## Normalization rules

Applied to the chosen frame, **in this order**:

| # | Rule | Example |
|---|---|---|
| 1 | Path-like tokens → last **two** segments | `/build/9f2a/src/app/cart.py` → `app/cart.py` |
| 2 | `0x…` → `0xADDR` | `0x7f8a3c2d1e40` → `0xADDR` |
| 3 | UUID → `UUID` | `6f0a1b2c-…` → `UUID` |
| 4 | `:N:N` → `:L:C` | `user.js:22:17` → `user.js:L:C` |
| 5 | `line N` → `line L` | `line 142` → `line L` |
| 6 | `:N` → `:L` | `Bar.java:57` → `Bar.java:L` |
| 7 | Hex run ≥ 8 → `HEX` | `deadbeefcafe` → `HEX` |
| 8 | Digit run ≥ 3 → `N` | `3000ms` → `Nms` |
| 9 | Collapse whitespace | |

`normalize_message` is the same list plus quoted-substring collapsing
(`'user_id'` → `'X'`), and without the line-number rules, which do not apply.

### Why two path segments

One segment (`cart.py`) would merge `app/handler.py` with `worker/handler.py`,
which are usually different bugs. The full path would split the same bug across
build machines, since CI checkout directories differ every run. Two is the
compromise that survives relocation while preserving module identity.

### Why rule 8 is not anchored on a word boundary

`3000ms` and `512MB` carry unit suffixes. `\b\d{3,}\b` never matches them, so
per-occurrence durations and sizes would leak into the identity and split one
bug into an issue per timeout value. `\d{3,}` unanchored fixes that. This is a
real bug the test suite caught during development.

## Worked example

```
Traceback (most recent call last):                        ← skipped, preamble
  File "/build/9f2a1c/src/app/cart.py", line 88, in total ← selected
    return sum(i.price for i in self.items)
TypeError: unsupported operand type(s) for +
```

```
selected  : File "/build/9f2a1c/src/app/cart.py", line 88, in total
rule 1    : File "app/cart.py", line 88, in total
rule 5    : File "app/cart.py", line L, in total
site      : frame:File "app/cart.py", line L, in total

digest    : sha256("checkout-api" ␟ "TypeError" ␟ 'frame:File "app/cart.py", line L, in total')
label     : err2issue-fp-v1-<first 12 hex>
```

## Properties, and the tests that hold them

`tests/test_fingerprint.py` is the executable form of this document. Each
guarantee below maps to named tests.

**Stable across** — line numbers moving, build-path prefixes, memory addresses,
UUIDs in temp paths, JS line:column changes, and per-request values in the
exception message when a stack is present.

**Distinct across** — different exception types, different services (the same
library bug in two services is two pieces of work, usually in two repos),
different functions in one file, and the same filename in different directories.

`test_v1_golden_vector` pins the exact normalized output. If it fails, the v1
rules changed — ship v2 rather than editing the test.

## The label

```
err2issue-fp-<version>-<digest>          err2issue-fp-v1-a3f9c21b8e04
```

GitHub caps label names at 50 characters. `err2issue-fp-` (13) + `v1-` (3) + 12
= **28**, leaving room for `v10+` and a longer digest without a breaking change.

This label is the **deduplication key** and the reason dedup is correct: it is
looked up with `GET /repos/{o}/{r}/issues?labels=<label>&state=all`, a strongly
consistent read of the issues table, rather than the eventually-consistent
search index (see [CHALLENGE.md](../CHALLENGE.md) §1).

> **Never remove this label from an issue.** err2issue will not find the issue
> and will file a duplicate on the next occurrence. Closing the issue is fine —
> that is how regression detection works.

## Versioning

The version lives *in the label*, which is what makes a rules change survivable.

To change the rules:

1. Bump `VERSION` in `src/err2issue/fingerprint.py` to `v2`.
2. Add the new rules to this document under a `## v2` heading. Leave the v1
   section intact — issues filed under v1 are still addressable.
3. Add a v2 golden vector test; keep the v1 one.
4. Note the change in [AGENTS.md](../AGENTS.md) under Gotchas.

**Migration behaviour.** After the bump, an error that was tracked under
`err2issue-fp-v1-abc` gets filed fresh under `err2issue-fp-v2-xyz`, because the
v1 label is no longer searched. The v1 issue stays open with its history intact.
This is a real cost, and it is why the version exists: the alternative is
silently orphaning every issue with no way to tell what happened.

**Do not edit the v1 rules in place.** A "small tweak" to normalization
re-fingerprints every error in every deployment simultaneously, with no signal
that anything happened.
