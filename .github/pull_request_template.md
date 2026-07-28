<!--
Title must follow Conventional Commits, e.g.
  fix(fingerprint): normalize unit-suffixed durations
  feat(routing): support regex service patterns
CI checks this. See CONTRIBUTING.md.
-->

## What and why

<!-- What changes, and what problem it solves. Link the issue: Fixes #123 -->

## Approach

<!-- Why this approach. Note anything you considered and rejected — that is
     usually the most useful part of a review. -->

## Verification

<!-- Paste real output, not a claim that it passed. -->

```
$ uv run pytest -q

```

- [ ] `uv run pytest` passes
- [ ] `uv run ruff check src tests` passes
- [ ] New behaviour has a test that fails without the change

## Contract impact

<!-- err2issue's value depends on two contracts staying stable. Tick anything
     this PR touches and say how existing installs are affected. -->

- [ ] **Fingerprint** (`src/err2issue/fingerprint.py`) — changing the
      normalization rules changes every error's identity. This requires a new
      `VERSION`, not an edit to `v1`. See [docs/FINGERPRINT.md](../docs/FINGERPRINT.md).
- [ ] **Issue format** (`src/err2issue/context.py`) — consumers parse the
      machine header and the `[xN]` title. See [docs/ISSUE_CONTRACT.md](../docs/ISSUE_CONTRACT.md).
- [ ] Neither — this change is invisible to existing installs.

## Scope notes

<!-- Anything related you deliberately did NOT do, and why. Sibling bugs you
     spotted but left alone belong here. -->
