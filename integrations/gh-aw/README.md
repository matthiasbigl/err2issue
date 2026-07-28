# gh-aw consumer

[GitHub Agentic Workflows](https://github.github.com/gh-aw/) take a different
approach from a plain Actions workflow: you write the agent's task in markdown
with a YAML frontmatter, and `gh aw compile` turns it into a locked `.lock.yml`
that GitHub actually runs.

The property that matters here is **safe outputs**. The agent never writes to
your repository directly. It emits proposed outputs, and the framework applies
only the kinds you declared, within the limits you set. If the frontmatter says
`create-pull-request` and `add-comment: max: 1`, that is the complete set of
things a run can do to your repository — regardless of what the agent decides.

That makes gh-aw the better choice when you are nervous about pointing an agent
at production error reports, which is a reasonable thing to be nervous about.

## Setup

### 1. Install the extension

```bash
gh extension install github/gh-aw
```

### 2. Add the workflow

```bash
mkdir -p .github/workflows
curl -fsSL -o .github/workflows/err2issue-autofix.md \
  https://raw.githubusercontent.com/matthiasbigl/err2issue/main/integrations/gh-aw/workflows/err2issue-autofix.md
```

### 3. Compile it

```bash
gh aw compile
```

This generates `.github/workflows/err2issue-autofix.lock.yml`. **Commit both
files** — the `.md` is the source you edit, the `.lock.yml` is what runs.

> Re-run `gh aw compile` after every edit to the markdown. Forgetting is the
> most common gh-aw mistake: the workflow keeps running the previous version and
> nothing indicates that your change had no effect.

### 4. Commit to the default branch

```bash
git add .github/workflows/err2issue-autofix.md .github/workflows/err2issue-autofix.lock.yml
git commit -m "ci: fix err2issue reports with an agentic workflow"
git push
```

GitHub only runs `issues`-triggered workflows from the default branch.

### 5. Try it

```bash
gh workflow run err2issue-autofix.lock.yml -f issue_number=<n>
gh run watch
```

## Configuration

Edit the frontmatter in the `.md` file, then recompile.

### When it runs

```yaml
on:
  issues:
    types: [opened, labeled]
  skip-if-no-match: 'label:err2issue'   # only issues err2issue filed
  reaction: eyes                        # acknowledge on the issue
  status-comment: true                  # post a run link while working
```

To require explicit opt-in, change the filter to a label you apply by hand:

```yaml
  skip-if-no-match: 'label:autofix'
```

`on:` also accepts `roles:`, `bots:`, `skip-bots:`, `stop-after:` (auto-disable
after a deadline), and `manual-approval:` for environment-gated runs.

### Which agent, and what it costs you to authenticate

```yaml
engine: copilot     # default
```

`claude`, `codex` and `gemini` are also supported. Claude means adding
`ANTHROPIC_API_KEY` to repository secrets; Codex means `OPENAI_API_KEY`.

**Copilot used to need a personal access token, and no longer does.** As of
[11 June 2026](https://github.blog/changelog/2026-06-11-agentic-workflows-no-longer-need-a-personal-access-token/)
an agentic workflow can authenticate Copilot inference with the built-in
`GITHUB_TOKEN`. The shipped workflow opts in:

```yaml
permissions:
  copilot-requests: write
```

Two things have to be true for it to work, and they are not up to this file:

1. Your organisation has **"Allow use of Copilot CLI billed to the organization"**
   enabled (Settings → Copilot → Policies). It is on by default if the existing
   Copilot CLI policy was.
2. You are on a current `gh aw` — `gh extension upgrade aw`.

When it works, AI credits are billed to the organisation rather than to a
person, and there is no long-lived secret to rotate. When it does not — a
personal repository with no organisation behind it, or an org that has not
enabled the policy — inference falls back to per-seat billing and needs a
`COPILOT_GITHUB_TOKEN` secret containing a fine-grained PAT. `gh aw compile`
prints which situation you are in.

### Cost ceilings

```yaml
max-ai-credits: 300         # per run
max-daily-ai-credits: 2000  # rolling 24 hours, per user per workflow
```

gh-aw's own default daily cap is 5000 AI Credits, roughly $50. The shipped
values are lower on purpose: fixing one bug is not a research project, and a
runaway loop is much cheaper to discover at 300 credits than at 5000. Look at
real runs with `gh aw logs` before raising them.

`on.roles` matters for the same reason — it is set to `[admin, maintainer, write]`
so that someone with only triage access cannot spend your credits by adding a
label.

### What it is allowed to do

This is the important block. The shipped configuration is:

```yaml
safe-outputs:
  create-pull-request:
    title-prefix: "[err2issue] "
    labels: [automated, err2issue-fix]
    draft: true
  add-comment:
    max: 1
  missing-tool:
```

`draft: true` means pull requests arrive as drafts — a reviewer marks them ready.
Drop it once you trust the output.

For a comment-only, read-only consumer, remove `create-pull-request` entirely:

```yaml
safe-outputs:
  add-comment:
    max: 1
  add-labels:
    max: 3
```

The agent then physically cannot open a pull request, whatever it concludes.

Other available output types include `update-issue`, `close-issue`,
`push-to-pull-request-branch`, `add-labels`, `remove-labels`, `assign-to-user`,
and `missing-data`. See the
[safe outputs reference](https://github.github.com/gh-aw/reference/safe-outputs/).

### Network

```yaml
network:
  allowed:
    - defaults
```

`defaults` covers the common package ecosystems. Add specific hosts if the
agent needs to reach an internal service to reproduce a bug. Ecosystem
identifiers are a fixed set (`node`, `python`, `go`, `containers`, …) and an
unrecognised one — `npm`, `pip`, `cargo` — is a compile error, not a silent
no-op.

## CI does not run on the agent's pull request

This one surprises everybody, and it is not a gh-aw decision. **A pull request
opened with the built-in `GITHUB_TOKEN` does not trigger `pull_request`,
`push`, or `pull_request_target` workflows.** GitHub blocks it to stop workflows
from recursively triggering each other. The agent's PR arrives with no checks
at all — which is exactly the PR you least want to merge unverified.

The fix is a repository secret named **`GH_AW_CI_TRIGGER_TOKEN`**, a
fine-grained PAT with **Contents: Read & Write**:

```bash
gh aw secrets set GH_AW_CI_TRIGGER_TOKEN --value "<pat>"
```

gh-aw picks that exact name up automatically and uses it to push one empty
commit to the PR branch after creating it, which starts CI normally. The shipped
workflow also references it explicitly:

```yaml
safe-outputs:
  create-pull-request:
    github-token-for-extra-empty-commit: ${{ secrets.GH_AW_CI_TRIGGER_TOKEN }}
```

If you configured a GitHub App for the workflow, use that instead and skip the
PAT entirely:

```yaml
    github-token-for-extra-empty-commit: app
```

**So: no PAT for inference, one small PAT for CI.** Those are different problems
with different fixes, and the June 2026 change solved only the first. If you are
content to review agent PRs by hand with no checks, delete the line and no token
is needed at all.

## gh-aw or claude-code-action?

Use gh-aw when you want the framework to bound the agent's blast radius, or when
you would rather not manage an API key. Use
[claude-code-action](../claude-code-action/) when you want direct control over
the agent loop and are already using Claude Code elsewhere.

Both read the same [issue contract](../../docs/ISSUE_CONTRACT.md). Switching
between them costs one workflow file.

## Running it for an organisation

One repository is the easy case. Rolling this out across an org — a shared
version of the workflow, pinned, updatable, with the cost and permission story
that goes with it — is [ORGANIZATIONS.md](ORGANIZATIONS.md).

## Verification status

**Compiled clean**: `gh aw compile` on gh-aw **v0.83.4** reports
`0 error(s), 0 warning(s)`, and err2issue's own CI compiles this file on every
run, so it cannot drift into being broken without the build going red.

That check is worth having because the compiler is a real validator, not a YAML
parser — an unknown frontmatter key, a wrong value type, or a key at the wrong
nesting level all fail the build.

## For agents

If you are an AI agent setting this up, read [AGENTS.md](AGENTS.md).
