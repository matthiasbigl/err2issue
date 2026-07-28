# Claude Code Action consumer

Two workflows that pick up err2issue issues:

| Workflow | What it does | Writes code? | Start here? |
|---|---|---|---|
| [`err2issue-triage.yml`](workflows/err2issue-triage.yml) | Investigates and posts an analysis comment + severity label | No | **Yes** |
| [`err2issue-autofix.yml`](workflows/err2issue-autofix.yml) | Reproduces, fixes, and opens a pull request | Yes | After triage looks good |

Run triage first for a week. It is cheap, it cannot break anything, and the
comments tell you whether the issue contract carries enough context for an agent
to be useful on *your* codebase before you let one open pull requests.

## Setup

### 1. Install the Claude GitHub App

```
https://github.com/apps/claude
```

Or, from a terminal with Claude Code installed, run `/install-github-app`, which
does this and the workflow setup interactively.

The app needs **Contents**, **Issues**, and **Pull requests** at read & write.

### 2. Add the API key

Repository → Settings → Secrets and variables → Actions → New repository secret:

| Name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your key from [console.anthropic.com](https://console.anthropic.com) |

### 3. Copy the workflow in

```bash
mkdir -p .github/workflows
curl -fsSL -o .github/workflows/err2issue-triage.yml \
  https://raw.githubusercontent.com/matthiasbigl/err2issue/main/integrations/claude-code-action/workflows/err2issue-triage.yml
```

Commit it to your **default branch** — GitHub only runs workflows from there for
`issues` events.

### 4. Prove it works

Pick an existing err2issue issue and run the workflow by hand:

```bash
gh workflow run err2issue-triage.yml -f issue_number=<n>
gh run watch
```

A triage comment should appear on the issue within a minute or two.

## Configuration

Everything you are likely to change is in the `env:` block at the top of each
workflow. No other edits are needed.

| Variable | Default | What it controls |
|---|---|---|
| `TRIGGER_LABEL` | `err2issue` | Only issues with this label are picked up. Set to something like `autofix` to opt in per issue instead of acting on everything err2issue files. |
| `MIN_OCCURRENCES` | `1` | Ignore errors seen fewer than this many times, read from the `[xN]` title prefix. **Raise this to 3–5 once you have real traffic** — it is the single most effective cost control. |
| `COMMAND_PHRASE` | `/err2issue fix` | Comment this on any err2issue issue to run on demand. |
| `AUTO_ON_OPEN` | `true` | Set to `false` to require a label or a comment before anything runs. |
| `CLAUDE_MODEL` | `claude-opus-5` | Model passed through to `claude_args`. |
| `MAX_TURNS` | `40` (autofix), `20` (triage) | Caps how long the agent works on one issue. |

### Common configurations

**Human-approved fixes only** — nothing runs until someone opts an issue in:

```yaml
env:
  TRIGGER_LABEL: autofix     # err2issue does not apply this; you add it by hand
  AUTO_ON_OPEN: "true"
```

**Only chronic errors, automatically** — ignore anything that has not recurred:

```yaml
env:
  TRIGGER_LABEL: err2issue
  MIN_OCCURRENCES: "10"
```

**On demand only** — no automatic runs at all; comment `/err2issue fix` to start:

```yaml
env:
  AUTO_ON_OPEN: "false"
```

## How the gate works

Both workflows split the decision into a cheap `gate` job and the expensive
agent job. The gate's decision and its reason are printed in the Actions log, so
"why didn't it run?" is answerable without guesswork:

```
decision: false (seen 1x, threshold is 3)
decision: true  (labelled err2issue, seen 7x)
```

Issue titles, labels, and comment bodies reach the gate script through the
environment, never through `${{ }}` interpolation into the shell. That matters:
on a public repository an issue title is attacker-controlled, and interpolating
it into a `run:` block is a shell-injection hole. The gate has been tested
against titles containing shell metacharacters.

## Cost

Both workflows consume GitHub Actions minutes and Claude API tokens per issue.

- Triage is bounded by `MAX_TURNS: 20` and reads only — typically a small number
  of file reads plus one comment.
- Autofix is bounded by `MAX_TURNS: 40` and does substantially more work.

`MIN_OCCURRENCES` is the main lever. err2issue's own suppression already
collapses a crash loop into one issue, so the two limits compose: err2issue
stops a storm from becoming a thousand issues, and `MIN_OCCURRENCES` stops a
one-off issue from becoming an agent run.

## Troubleshooting

**Nothing ran.** Check the `gate` job in the Actions tab — it prints why. The
usual causes are the workflow not being on the default branch, or the issue not
carrying `TRIGGER_LABEL`.

**"Resource not accessible by integration".** The `permissions:` block was
edited, or the Claude GitHub App is not installed on this repository.

**The agent removed the fingerprint label.** Re-add it:
`gh issue edit <n> --add-label err2issue-fp-v1-<hash>`. Without it err2issue
cannot find the issue and will file a duplicate on the next occurrence. The
shipped prompts tell the agent not to do this.

## For agents

If you are an AI agent setting this up rather than a human, read
[AGENTS.md](AGENTS.md) — it is the same procedure written as an executable
checklist with verification steps.
