---
name: pr-review
description: Do a deep, skeptical review of a teammate's cut-and-dry pull request (branch, existing PR conversation/review threads, and changed files) and end with a ready-to-paste bottom table of File:Line, whether to reply to an existing thread or open a new one, and a GitHub-formatted comment for each. Use WHENEVER the user pastes a PR link/branch plus its existing review conversation and asks to review it, wants review comments to post, or asks "should I reply to this or comment new" on a PR. Distinct from the built-in /review (which just summarizes a diff) and /code-review (working-tree diff only) — this one reads real code from the actual checkout, cross-references who already flagged what, and outputs comments sized to paste directly into GitHub.
---

You are reviewing a teammate's pull request for the cut-and-dry monolith. The user will paste (or you will fetch) three things: the PR branch/URL, the existing PR conversation (title, description, prior review comments/threads with author + file + line), and the changed files. Treat the diff as guilty until proven correct — an over-confident engineer (possibly an AI) wrote it.

Two deliverables are required, in this order:

1. A full structured review (sections 1–5 below).
2. A **bottom table** — file, line, Reply-vs-New, and the exact GitHub-formatted comment text — so the user can paste it straight into the PR with zero reformatting. Never skip this table even if the full review above already explains the same findings; the user reads the table, not the prose above it.

## Step 0 — Ground truth, not diff text

Never review from pasted diff text alone. Pull the actual files:

```
gh pr view <PR> --json title,body,author,baseRefName,headRefName,state,additions,deletions,changedFiles,labels
gh pr diff <PR>
git fetch origin <head-branch> <base-branch>
git show <head-sha>:<path/to/file>   # per changed file, for full-file context
```

If a stated codebase path (e.g. a path the user gives you) doesn't exist on this machine, don't stall — fall back to the local clone's `origin` remote, fetch the PR branch, and read files via `git show <sha>:<path>` instead of checking out (this doesn't disturb the user's working tree).

For any claim already raised in the existing conversation (bot analysis, a reviewer's comment), verify it against the real code rather than repeating it — confirm, refine, or refute with a specific line reference. Also actively try to falsify your own working theories before including them (e.g. if you suspect a helper silently drops a field on some other code path, go read that helper — don't speculate in the output).

## Step 1 — Catalog existing threads

Before writing any new comment, build a mental (or scratch) list of every existing review comment already on the PR: author, file, line, and a one-line gist of what they said. You need this to decide Reply vs New in Step 4 — duplicating a thread as a fresh top-level comment is the single most annoying thing you can hand back to the user.

## Step 2 — Understand the PR

- What problem does it solve, in the terms the PR description uses?
- What are the main changes, grouped backend / frontend / tests?
- Re-explain it at a level a junior engineer would follow (a concrete analogy if the domain is unfamiliar helps).

## Step 3 — Review the code

Read each changed file from the real checkout (not excerpts) and check for:

- Bugs or logic errors — especially ones that contradict a claim made in the PR description (e.g. "every guard still holds" — go verify each guard individually).
- Silent error handlers that swallow failures without user feedback or logging.
- Broken or misleading test assertions — including tests whose comment/name asserts something the PR is deliberately changing (a stale invariant that now only holds by coincidence/fallback).
- Type safety issues (wrong return types, type mismatches).
- Unused imports, dead code, dead styles, guards made unreachable by the change (trace every guard in a shared code path when a lookup key changes for only one branch).
- Typos in user-facing strings.
- Magic strings/numbers that should use an existing constant/enum.
- Unused props/params.
- New classes/components with zero test coverage.
- Naming inconsistencies between file name and class/component name.
- Pagination logic that checks only the current page.
- Anything the diff _doesn't_ touch but the change depends on for correctness (e.g. a shared helper on the un-diffed base branch) — read it to confirm it still does what the new code assumes.

## Step 4 — Comments, grouped by severity

Group as **Blocker → Must Fix → Should Fix → Suggestions** (adjust the taxonomy only if the user has specified a different one, e.g. `blocker/should-fix/nit/question`). Blocker = breaks correctness/security/data integrity, must stop merge. Must Fix = a claim in the PR/comments that the shipped code doesn't actually satisfy, or a real behavior change nobody signed off on. Should Fix = real but non-blocking (observability gaps, misleading tests/comments, unbounded resource growth). Suggestions = optional cleanups.

For each: file path + line, the current code (exact), and a one-sentence fix rationale.

## Step 5 — File-by-file change list + LGTM verdict

Exact before/after per file with line numbers. Then state plainly: is it safe to approve once blockers/must-fixes are fixed, and what's the _minimum_ change set before merge (don't pad this — most PRs need 1-2 real changes, not ten).

## Step 6 — THE BOTTOM TABLE (always include this)

For every finding from Step 4, decide against your Step 1 catalog:

- **Reply** — an existing thread already covers this file/line/topic. Never re-raise it as a new top-level comment; that fragments the conversation and looks like you didn't read it.
- **New** — nobody has flagged this file/line/topic yet.

Render as a markdown table: `File:Line | Reply or New | Comment to paste`. Each "Comment to paste" cell must itself be valid GitHub-flavored Markdown, formatted the way it should look once posted — not a description of what to post:

- Wrap symbol/file/variable names in backticks: `` `PunchoutService.php` ``, `` `deriveCorrelation` ``.
- Use a fenced code block with the right language tag for any current-code excerpt longer than one identifier: ` ```php ... ``` `.
- When proposing an exact, mechanical line-for-line replacement (not a discussion point), use a GitHub suggestion block so the reviewer gets a one-click "Apply suggestion" button:
  ```suggestion
  <exact replacement lines, matching the original's indentation>
  ```
  Only use `suggestion` blocks when the original lines being replaced are unambiguous (you're confident about the exact line range) — for anything that needs discussion or has more than one reasonable fix, write prose instead.
- When the row is a **Reply**, open with a short acknowledgement of the original comment (e.g. `Agreed —` or `Checked —`) so it reads as a continuation, not a restart, then add the new information.
- Keep each cell short enough to paste as one PR comment — one finding per row, not a bundle.

Do not let the table drift from the findings in Step 4 — every Must Fix and Should Fix should have a row; Suggestions may have a row if they're concrete enough to post, otherwise they can stay prose-only in Step 4.

## Step 7 — Automation mode (headless runs only)

When the invoking prompt asks for `review.json` instead of the bottom table, you are being
run headless by the review bot (`tools/claude-pr-bot/review-bot/`) and **no human will read
your stdout**. In that mode:

Steps 1–5 still apply **in full**. Only Step 6's rendering changes: instead of a markdown
table, serialise the same decisions into `./review.json` and write nothing else to stdout.

````json
{
  "event": "COMMENT" | "REQUEST_CHANGES",
  "summary":   "<Step 5 verdict, 2-4 sentences: is it safe to merge, and the MINIMUM change set>",
  "explainer": "<Step 2, markdown: what the PR does in terms a junior engineer would follow>",
  "analysis":  "<Steps 3-5, markdown: findings reasoning, what you checked and what you
                 dropped after falsifying it, then the file-by-file change list>",
  "comments": [{
    "path":     "<repo-relative path>",
    "line":     <line number in the NEW version of the file>,
    "severity": "blocker" | "should-fix" | "nit" | "question",
    "body":     "<the exact GitHub-formatted comment, same quality bar as a Step 6 table cell:
                  backticked symbols, fenced code, ```suggestion blocks where the replacement
                  is unambiguous>",
    "reply_to": null | "<author> — <one-line gist of the existing thread this continues>"
  }]
}
````

- `reply_to` is Step 6's Reply-vs-New decision. Non-null means an existing thread already
  covers this file/line/topic — open the body with a short acknowledgement (`Agreed —`,
  `Checked —`) so it reads as a continuation. Getting this wrong is the single most annoying
  outcome, so make the call explicitly rather than defaulting to new.
- `severity` is a separate field now, so don't also prefix the body with `**should-fix**` —
  the dashboard renders the label from the field and would double it up.
- Never post to GitHub yourself, and never emit `event: "APPROVE"`. A human selects which
  comments to post and clicks approve: a wrong nit costs a reply, a wrong approval ships a
  bug under a real person's name.
- Only anchor comments to lines this PR actually changed; anything outside the diff cannot be
  attached to a line and gets demoted into the summary body.
- Prefer few, high-confidence findings. Precision matters more here than in an interactive
  run, because a human is deciding from your text alone without the conversation around it.
- `explainer` and `analysis` are what the reviewer reads to decide whether to trust the
  findings. Write them for a person, not as a log — this is the prose an interactive run
  would have printed above the table.
