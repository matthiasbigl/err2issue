# AGENTS.md — set up the Claude Code Action consumer

Instructions for an AI agent asked to wire err2issue issues into Claude Code
Action in a repository. Follow the steps in order. Each has a verification step;
do not report success until it passes.

## Preconditions — check these first, do not assume

```bash
gh auth status                      # authenticated?
gh repo view --json nameWithOwner,defaultBranchRef,hasIssuesEnabled
gh secret list | grep -i anthropic  # is ANTHROPIC_API_KEY already set?
```

Stop and ask the human if any of these hold:

- Issues are disabled on the repository. Nothing here can work; err2issue cannot
  file into it either.
- `ANTHROPIC_API_KEY` is absent. **You cannot create it** — it is the human's
  API key. Ask them to add it at Settings → Secrets and variables → Actions, and
  wait.
- The Claude GitHub App is not installed. Direct them to
  <https://github.com/apps/claude>. You cannot install it on their behalf.

## Step 1 — choose triage or autofix

Default to **triage** unless the human explicitly asked for automated fixes.

| Ask them | Then install |
|---|---|
| "Should the agent open pull requests, or only analyse and comment?" | `err2issue-triage.yml` for analysis, `err2issue-autofix.yml` for pull requests |

Reasoning to give them if they are unsure: triage is read-only, costs less, and
shows whether the issue contract carries enough context to be useful on this
codebase before anything writes code.

## Step 2 — install the workflow

```bash
mkdir -p .github/workflows
curl -fsSL -o .github/workflows/err2issue-triage.yml \
  https://raw.githubusercontent.com/matthiasbigl/err2issue/main/integrations/claude-code-action/workflows/err2issue-triage.yml
```

If the network is unavailable, copy the file from
`integrations/claude-code-action/workflows/` in the err2issue repository
verbatim. **Do not write it from memory** — the gate script's quoting is
load-bearing for shell-injection safety.

Verify:

```bash
python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/err2issue-triage.yml')); print('YAML OK')"
```

## Step 3 — configure the trigger

Edit only the `env:` block. Ask the human these two questions rather than
guessing, because the right answers depend on traffic you cannot see:

1. **"Should this run on every err2issue issue, or only ones you opt in?"**
   - Every issue → leave `TRIGGER_LABEL: err2issue`.
   - Opt in → set `TRIGGER_LABEL: autofix`, and tell them they add that label by
     hand to the issues they want handled.

2. **"How many times should an error recur before the agent looks at it?"**
   - Recommend `3` if they have no opinion. Explain that `1` means every
     transient blip triggers a run, and the count comes from the `[xN]` prefix
     err2issue puts in the title.
   - Set `MIN_OCCURRENCES` accordingly.

Do not change `permissions:`, the `concurrency:` group, or the gate script.

## Step 4 — commit to the default branch

```bash
git checkout "$(gh repo view --json defaultBranchRef -q .defaultBranchRef.name)"
git add .github/workflows/err2issue-triage.yml
git commit -m "ci: triage err2issue reports with Claude Code"
git push
```

GitHub only runs `issues`-triggered workflows from the default branch. A
workflow on a feature branch will never fire, and this failure is silent — there
is no error to see. If the repository requires pull requests, open one and tell
the human the workflow is inactive until it merges.

## Step 5 — verify against a real issue

Do not declare success on a green syntax check. Exercise it:

```bash
gh issue list --label err2issue --limit 5
gh workflow run err2issue-triage.yml -f issue_number=<n>
sleep 10 && gh run list --workflow=err2issue-triage.yml --limit 1
gh run watch
```

Then confirm the outcome actually landed:

```bash
gh issue view <n> --comments | tail -40
```

If no err2issue issue exists yet, say so and stop — the setup is complete but
unproven, and the human should know the difference.

## Failure modes and what they mean

| Symptom | Cause | Fix |
|---|---|---|
| Workflow never triggers | Not on the default branch | Merge it there |
| `gate` job outputs `run=false` | Read its log line — it prints the reason | Adjust `TRIGGER_LABEL` or `MIN_OCCURRENCES` |
| `Resource not accessible by integration` | `permissions:` edited, or App not installed | Restore the block; install the App |
| `ANTHROPIC_API_KEY` not found | Secret missing or misnamed | Human adds it; the name is exact |
| Agent ran but did nothing useful | Issue lacked a stack trace | Not a workflow problem — check the instrumentation is setting `exception.stacktrace` |

## Rules

- **Never commit an API key.** If you find one in a workflow file or in the
  repository, stop, tell the human, and recommend rotating it.
- **Never widen `permissions:`.** Triage is deliberately `contents: read`. An
  agent that can only comment cannot damage the repository.
- **Never remove an `err2issue-fp-v1-*` label** from any issue. It is the
  deduplication key; removing it causes duplicate issues.
- **Do not invent configuration keys.** The valid `env:` names are exactly those
  in the shipped file. `claude-code-action@v1` inputs are `prompt`,
  `claude_args`, `anthropic_api_key`, `github_token`, `trigger_phrase`,
  `plugin_marketplaces`, `plugins`, `use_bedrock`, `use_vertex`. Anything else
  is silently ignored.
- **Report honestly.** If you completed steps 1–4 but could not verify step 5,
  say exactly that. "Set up and verified against issue #12" and "set up but not
  yet exercised" are different claims.
