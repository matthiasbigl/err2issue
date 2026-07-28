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

### Which agent

```yaml
engine: copilot     # default; no extra secret on GitHub-hosted runners
```

`claude` and `codex` are also supported. Using Claude means adding
`ANTHROPIC_API_KEY` to repository secrets:

```yaml
engine: claude
```

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
agent needs to reach an internal service to reproduce a bug.

## gh-aw or claude-code-action?

Use gh-aw when you want the framework to bound the agent's blast radius, or when
you would rather not manage an API key. Use
[claude-code-action](../claude-code-action/) when you want direct control over
the agent loop and are already using Claude Code elsewhere.

Both read the same [issue contract](../../docs/ISSUE_CONTRACT.md). Switching
between them costs one workflow file.

## Verification status

The frontmatter here parses as valid YAML and uses documented fields only, but
**this workflow has not been run through `gh aw compile` by the err2issue
maintainers** — the extension was not installable in the environment where it
was written. Run `gh aw compile` before committing, and open an issue if it
rejects anything.

## For agents

If you are an AI agent setting this up, read [AGENTS.md](AGENTS.md).
