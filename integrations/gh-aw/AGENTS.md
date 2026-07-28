# AGENTS.md — set up the gh-aw consumer

Instructions for an AI agent asked to wire err2issue issues into a GitHub
Agentic Workflow. Follow in order; each step has a verification you must run.

## Preconditions

```bash
gh auth status
gh extension list | grep gh-aw || gh extension install github/gh-aw
gh aw --help                      # must succeed before continuing
gh repo view --json nameWithOwner,defaultBranchRef,hasIssuesEnabled
```

Stop and ask the human if:

- `gh extension install github/gh-aw` fails. Report the exact error. Do not
  hand-write a `.lock.yml` as a workaround — it is generated output and
  hand-editing it will be overwritten and may be wrong.
- Issues are disabled on the repository.

## Step 1 — install the source workflow

```bash
mkdir -p .github/workflows
curl -fsSL -o .github/workflows/err2issue-autofix.md \
  https://raw.githubusercontent.com/matthiasbigl/err2issue/main/integrations/gh-aw/workflows/err2issue-autofix.md
```

Offline: copy `integrations/gh-aw/workflows/err2issue-autofix.md` from the
err2issue repository verbatim.

Verify the frontmatter parses:

```bash
python3 - <<'EOF'
import yaml
text = open('.github/workflows/err2issue-autofix.md').read()
assert text.startswith('---\n'), 'frontmatter must open the file'
_, fm, _ = text.split('---\n', 2)
print(list(yaml.safe_load(fm)))
EOF
```

## Step 2 — decide the safety envelope with the human

This is the decision that matters in gh-aw, so do not pick it silently. Ask:

> "Should the agent be able to open pull requests, or only comment and label?"

**Comment only** — replace the `safe-outputs:` block with:

```yaml
safe-outputs:
  add-comment:
    max: 1
  add-labels:
    max: 3
  missing-tool:
```

The agent then cannot open a pull request no matter what it concludes. This is
the right default for a first deployment.

**Pull requests allowed** — keep the shipped block. Keep `draft: true` unless
they explicitly ask for ready-for-review PRs.

Then ask:

> "Run on every err2issue issue, or only ones you opt in by label?"

- Every issue → leave `skip-if-no-match: 'label:err2issue'`.
- Opt in → change to `skip-if-no-match: 'label:autofix'` and tell them they must
  add that label by hand.

If they want Claude instead of Copilot, set `engine: claude` **and** tell them
they must add `ANTHROPIC_API_KEY` to repository secrets. You cannot add it.

## Step 3 — compile

```bash
gh aw compile
ls -la .github/workflows/err2issue-autofix.lock.yml
```

**This step is mandatory and is the one agents skip.** The `.md` is source; the
`.lock.yml` is what GitHub runs. If you edit the markdown again later, you must
recompile — otherwise the old version keeps running and nothing tells you.

If `gh aw compile` reports an error, fix the frontmatter and recompile. Do not
proceed with a failed compile, and do not edit the `.lock.yml` by hand.

## Step 4 — commit both files to the default branch

```bash
git checkout "$(gh repo view --json defaultBranchRef -q .defaultBranchRef.name)"
git add .github/workflows/err2issue-autofix.md .github/workflows/err2issue-autofix.lock.yml
git commit -m "ci: fix err2issue reports with an agentic workflow"
git push
```

Both files. Committing only the `.md` means nothing runs; committing only the
`.lock.yml` means the next person cannot regenerate it.

## Step 5 — verify against a real issue

```bash
gh issue list --label err2issue --limit 5
gh workflow run err2issue-autofix.lock.yml -f issue_number=<n>
gh run watch
gh issue view <n> --comments | tail -40
```

If no err2issue issue exists yet, say the setup is complete but unproven. Do not
claim it works.

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `unknown command "aw"` | Extension not installed | `gh extension install github/gh-aw` |
| Workflow never triggers | `.lock.yml` not committed, or not on the default branch | Commit both files to the default branch |
| Edits have no effect | Forgot `gh aw compile` | Recompile and commit the regenerated `.lock.yml` |
| Agent produced no PR | `create-pull-request` not in `safe-outputs` | Intended, if you chose comment-only |
| `skip-if-no-match` filtered everything | Label mismatch | Confirm the issue actually carries the label |

## Rules

- **Always `gh aw compile` after editing the markdown.** Then commit the
  regenerated lock file.
- **Never hand-edit `.lock.yml`.** It is generated.
- **Never widen `safe-outputs:` without asking.** It is the entire safety model:
  what is not declared cannot happen.
- **Never remove an `err2issue-fp-v1-*` label** from an issue. It is the
  deduplication key; removing it causes duplicate issues.
- **Do not invent frontmatter keys.** Valid top-level keys are `on`,
  `permissions`, `engine`, `tools`, `network`, `safe-outputs`, `timeout-minutes`,
  `runs-on`, `run-name`. Check
  <https://github.github.com/gh-aw/reference/frontmatter/> rather than guessing;
  an unknown key fails the compile, which is at least loud.
- **Report honestly** whether you verified step 5 or only completed setup.
