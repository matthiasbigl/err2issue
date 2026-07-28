# AGENTS.md — set up the gh-aw consumer

Instructions for an AI agent asked to wire err2issue issues into a GitHub
Agentic Workflow. Follow in order; each step has a verification you must run.

## Preconditions

```bash
gh auth status
gh extension list | grep gh-aw || gh extension install github/gh-aw
gh extension upgrade aw           # REQUIRED — see below
gh aw version                     # must succeed before continuing
gh aw doctor                      # diagnoses auth and repository setup
gh repo view --json nameWithOwner,defaultBranchRef,hasIssuesEnabled
```

**Upgrade, do not just install.** Token-based Copilot inference — the thing that
removes the personal access token requirement — needs a current CLI. On an old
one the workflow below still compiles and then asks for a PAT at run time, which
looks like a configuration mistake rather than a stale tool.

If `gh` is unavailable, the standalone installer works and needs no `gh`:

```bash
curl -sL https://raw.githubusercontent.com/github/gh-aw/main/install-gh-aw.sh | bash
```

Stop and ask the human if:

- Installing fails. Report the exact error. Do not hand-write a `.lock.yml` as a
  workaround — it is generated output, it will be overwritten, and it may be
  wrong.
- Issues are disabled on the repository.
- The organisation restricts which actions may run and `github/gh-aw@*` is not
  on the allow-list (Settings → Actions → Policies). You cannot change this;
  an org admin must. Every compiled workflow fails to run until they do.

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

## Step 3 — settle the two token questions

These are separate problems with separate answers, and conflating them is the
most common way to get this wrong.

**Inference — usually no token.** The shipped workflow declares
`permissions: copilot-requests: write`, which bills Copilot inference to the
organisation through the built-in `GITHUB_TOKEN`. Verify the org policy is on:

> "Is 'Allow use of Copilot CLI billed to the organization' enabled under
> Settings → Copilot → Policies?"

If yes, there is nothing to add. If no — or the repository is personal and has
no organisation behind it — the run falls back to per-seat billing and needs a
`COPILOT_GITHUB_TOKEN` secret holding a fine-grained PAT. **You cannot create
it.** Ask, and say plainly that the setup is incomplete until they do.

**CI on the agent's pull request — one small token.** A pull request opened
with the built-in `GITHUB_TOKEN` does not trigger `pull_request` or `push`
workflows; GitHub blocks it to prevent recursion. So the agent's PR arrives with
no checks. Ask:

> "Should the agent's pull requests run your CI? That needs one repository
> secret — a fine-grained PAT with Contents: Read & Write."

- Yes → they create it, then
  `gh aw secrets set GH_AW_CI_TRIGGER_TOKEN --value "<pat>"`. That exact name is
  picked up automatically.
- Already have a GitHub App on the workflow → set
  `github-token-for-extra-empty-commit: app` and no PAT is needed.
- No → delete the `github-token-for-extra-empty-commit:` line, and **tell them
  explicitly that agent pull requests will show no checks**. That is a real cost
  of the choice, not a detail.

## Step 4 — compile

```bash
gh aw compile
ls -la .github/workflows/err2issue-autofix.lock.yml
```

**This step is mandatory and is the one agents skip.** The `.md` is source; the
`.lock.yml` is what GitHub runs. If you edit the markdown again later, you must
recompile — otherwise the old version keeps running and nothing tells you.

The compiler is a real validator, not a YAML parser: unknown keys, wrong value
types, and keys at the wrong nesting level all fail the build. So a clean
compile is meaningful evidence. **Treat warnings as errors** — a missing
toolset permission compiles fine and then fails at run time.

`gh aw validate` checks without writing a lock file, which is useful mid-edit.

If `gh aw compile` reports an error, fix the frontmatter and recompile. Do not
proceed with a failed compile, and do not edit the `.lock.yml` by hand.

## Step 5 — commit both files to the default branch

```bash
git checkout "$(gh repo view --json defaultBranchRef -q .defaultBranchRef.name)"
git add .github/workflows/err2issue-autofix.md .github/workflows/err2issue-autofix.lock.yml
git commit -m "ci: fix err2issue reports with an agentic workflow"
git push
```

Both files. Committing only the `.md` means nothing runs; committing only the
`.lock.yml` means the next person cannot regenerate it.

## Step 6 — verify against a real issue

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
| Agent's PR has no CI checks | PRs opened with `GITHUB_TOKEN` do not trigger workflows | Set the `GH_AW_CI_TRIGGER_TOKEN` secret (step 3) |
| Run asks for `COPILOT_GITHUB_TOKEN` | Org Copilot CLI billing policy off, or stale CLI | Enable the policy, or `gh extension upgrade aw` |
| Every run fails before the agent starts | `github/gh-aw@*` not on the org's allowed-actions list | An org admin adds it |
| Run stopped at a credit cap | `max-ai-credits` / `max-daily-ai-credits` reached | Inspect with `gh aw logs`, then raise deliberately |
| Nothing triggers for one contributor | `on.roles` excludes them (default: admin, maintainer, write) | Intended; widen only on purpose |

## Rules

- **Always `gh aw compile` after editing the markdown.** Then commit the
  regenerated lock file.
- **Never hand-edit `.lock.yml`.** It is generated.
- **Never widen `safe-outputs:` without asking.** It is the entire safety model:
  what is not declared cannot happen.
- **Never remove an `err2issue-fp-*` label** from an issue, whatever version it
  carries. It is the deduplication key; removing it causes duplicate issues.
  Issues predating a fingerprint version bump keep the older label and are
  addressable only through it.
- **Never add a repository write permission.** gh-aw workflows must not declare
  `contents: write`, `issues: write`, or `pull-requests: write` — writes go
  through `safe-outputs`, which is the whole safety model. `copilot-requests:
  write` is not an exception to this: it authorises model inference, not
  repository access.
- **Do not invent frontmatter keys, and do not trust a short list of them
  either.** The set is large and moves — `description`, `source`, `imports`,
  `strict`, `roles`, `max-ai-credits`, `concurrency`, `env`, `cache`,
  `mcp-servers`, `steps` and many more are all valid. The authority is
  <https://github.github.com/gh-aw/reference/frontmatter/> and, more reliably,
  `gh aw compile` — it rejects unknown keys, wrong types, and keys nested at the
  wrong level. Try it rather than reasoning about it.
- **Report honestly** whether you verified step 6 or only completed setup.

## Rolling this out to more than one repository

Do not copy this file into repository after repository. gh-aw has a
distribution model — a central repository, `gh aw add`/`update`/`deploy`,
version pinning, and a shared policy file pulled in with `imports:` — described
in [ORGANIZATIONS.md](ORGANIZATIONS.md). Read it before the third copy.
