<!--
Template for a pull request opened by an agent in response to an err2issue
report. Use it with:
  gh pr create --template agent_fix.md

Title must follow Conventional Commits, scoped to the module where the bug was:
  fix(cart): handle items with no price when computing total
-->

Fixes #<!-- issue number -->

## What failed

<!-- The user-visible symptom, and how often. The `[xN]` prefix in the
     err2issue title is the occurrence count — say it, because it tells the
     reviewer whether this was a rare input or a broken common path. -->

- **Service:**
- **Exception:**
- **Occurrences:** `[xN]`
- **Fingerprint:** `v2:`

## Root cause

<!-- Why it happened, naming the file and function. One or two sentences.
     If a specific commit introduced it, link it — that is the single most
     useful fact in this PR. -->

## The fix

<!-- What changed and why this approach rather than an alternative.

     If you suppressed the exception rather than fixing the cause, stop and
     reconsider: a swallowed exception stops being reported, so err2issue goes
     quiet and the bug becomes invisible instead of fixed. -->

## Verification

<!-- Real output. Not "tests pass". -->

**Test that reproduces the bug** (fails before this change):

```

```

**Suite output:**

```
$

```

- [ ] A new test fails without this change and passes with it
- [ ] The full test suite passes
- [ ] Linter and type checker pass

## Scope notes

<!-- Required. Answer both: -->

**Does this same mistake exist elsewhere?**
<!-- You were asked to search for the pattern, not just the line. Say what you
     found, even if the answer is "checked the other three call sites in
     cart.py, they all guard correctly". -->

**What did you deliberately not touch?**
<!-- Unrelated problems noticed along the way, refactors resisted. Listing them
     here is more useful than fixing them in this PR. -->

## Reviewer notes

<!-- Anything you are unsure about. If confidence in the root cause is less
     than high, say so plainly here — a flagged uncertainty is far cheaper than
     a silently wrong fix. -->
