#!/usr/bin/env bash
# run-qa.sh <pr-number> — build a QA test guide for one PR with the pr-qa-guide skill.
#
# Spawned detached by prbot-server.py from the QA guide page. Writes progress to
# $STATE/<pr>/qa.status and the finished guide (GitHub-flavored markdown) to $STATE/<pr>/qa.md.
# Never writes to GitHub — it only reads the PR (diff, review threads, history) and produces a
# guide the human hands to QA.
set -uo pipefail
. "$(dirname "$0")/lib-common.sh"
require_env

PR="${1:?usage: run-qa.sh <pr-number>}"
DIR="$STATE/$PR"
mkdir -p "$DIR"
# A QA guide lock separate from the review lock, so a QA build and a review can't collide but a
# review being open never blocks generating a guide.
exec 9>"$DIR/.qa.lock"
flock -n 9 || { echo "QA guide for #$PR already running"; exit 0; }

status() { echo "$1" > "$DIR/qa.status"; echo "[QA #$PR] $1"; }
fail() { status "failed: $1"; exit 1; }

have_free_mem || fail "not enough free memory to start"

status "fetching the PR"
meta=$(gh pr view "$PR" --repo "$REPO" \
        --json headRefName,headRefOid,title,url,author 2>/dev/null) || fail "PR not found"
branch=$(echo "$meta" | jq -r .headRefName)
# Cache identity so the QA page keeps the title after the PR leaves the review queue.
echo "$meta" | jq --arg n "$PR" '{number:($n|tonumber), title, url, author:.author.login}' \
  > "$DIR/qa_meta.json"

status "checking out the branch"
git -C "$BASE" fetch -q origin "$branch" || fail "could not fetch $branch"
wt="$WT/qa-$PR"
git -C "$BASE" worktree remove --force "$wt" 2>/dev/null || true
git -C "$BASE" worktree add -q --force -B "qa-$PR" "$wt" "origin/$branch" \
  || fail "could not create worktree"

# One heavy agent at a time, box-wide — share the review lock so a guide + a review don't OOM.
status "queued — waiting for another job to finish"
exec 8>"$ROOT/review.lock"
flock 8

status "building the QA guide"
rm -f "$wt/qa.md"
PROMPT="Use the pr-qa-guide skill to build a QA test guide for PR #$PR of $REPO.

IMPORTANT — you are running headless: there is NO Artifact tool and no publishing here. Do NOT try
to publish an artifact or load artifact-design. Instead do the skill's evidence-gathering and
analysis exactly as written, then write the finished guide to ./qa.md as GitHub-flavored markdown,
following the skill's structure and its 'write for a tester' rules:
- a one-line what-this-is and the ticket/PR link;
- Setup / prerequisites (environment, flags, how a tester triggers it, and any environment trap
  called out prominently);
- a Surface matrix as a markdown table (where the feature must and must NOT appear);
- risk-tiered manual test cases grouped P0 / P1 / P2, each numbered and self-contained as a
  '- [ ]' checkbox item with the data needed, ordered steps, and an explicit Pass and Fail;
- a 'Known — please don't file these' section of intentional limitations / out-of-scope surfaces.
Keep every line traceable to the diff, review threads or code, and usable by a tester who has
never opened the repo. Do not post anything to GitHub."

(cd "$wt" && timeout 25m claude -p "$PROMPT" \
  --allowedTools "Bash Read Glob Grep Write" < /dev/null) >"$DIR/qa.log" 2>&1

[ -s "$wt/qa.md" ] || fail "the agent produced no qa.md (see qa.log)"
cp "$wt/qa.md" "$DIR/qa.md"
git -C "$BASE" worktree remove --force "$wt" 2>/dev/null || true
status "done"
echo "[QA #$PR] done ($(wc -l < "$DIR/qa.md") lines)"
