# Robin — Multi-Reviewer Redundancy & Trust Build Plan (v2)

Purpose: a spec you can hand directly to Claude to implement in Robin. Goal: let several
reviewers be assigned to the same PR (required by how Cut+Dry works today) without wasteful
duplicate analysis, while turning "several independent reviews" into an *actual, measurable*
confidence signal instead of an assumed one — and producing credible data to pitch wider
adoption after the one-week pilot.

Builds on Robin's existing architecture: Python stdlib backend, per-PR **per-reviewer**
file-based state (`STATE/<pr>/users/<login>/…`), React SPA, per-user GitHub identity + Claude
account, existing Learnings / Skills / token-usage-chip features. No new infrastructure.

---

## What changed from v1 (decisions now resolved)

v1 was a strong plan with three problems. This v2 resolves them:

1. **Phase 1 no longer re-shares reviews.** Robin deliberately un-shared reviews (each reviewer
   runs their own, owns it, and one running never blocks/overwrites another). v1's cross-reviewer
   cache quietly reversed that. **v2 scopes the cache to per-user re-runs only** — reusing *your
   own* identical re-run on the same commit. Cross-user sharing is explicitly deferred (see
   Phase 1 "Deferred").
2. **The confidence signal counts independent *configurations*, not runs.** Treating a cache hit —
   or two reviewers who happened to pick the identical config — as two confirmations is
   *pseudoreplication*: correlated observations inflating confidence. v2 makes independence a
   **weight, not a flag** (Phase 3).
3. **Phase 5 (skill audit trail) is GitOps-on-save, and it is not free.** The team-default skill
   lives in mutable box state (`$ROOT/skills/_global.md`), not git. v2 names the four moving parts
   required to give it a real commit history.

One design rule ties it together: **the confidence signal may only count independent
configurations, and independence is a weight, not a flag.** That single rule resolves the old
Phase-1↔Phase-3 contradiction, the Phase-2↔Phase-3 tension, and the pseudoreplication risk at once.

---

## Phase 1 — Per-user re-run cache (kill *self*-duplicate spend)

**Problem:** A reviewer who re-runs the *same* configuration on the *same* commit (e.g. reopened
the trigger screen, double-clicked, or re-ran without changing anything) pays and waits for a
byte-identical answer.

**Design — content addressing, scoped to the reviewer.** The cache key is a hash of everything
that determines the output *plus the reviewer*, so a hit is provably that same reviewer's same
review. No invalidation logic: change any input and the key changes, the old entry is simply never
looked up again. (Content-addressed / idempotency-key pattern.)

**Implementation:**

1. `cache_key = sha256(login + pr_head_sha + skill_content_hash + effort + focus_text + model + schema_version)`
   - `login` is in the key — this cache is **per reviewer**. It never serves one person's
     generation to another.
   - `skill_content_hash` — hash the *resolved* skill text (team-default or personal, whichever is
     active), not the skill's name. Editing the team skill changes the hash automatically; nothing
     is manually invalidated.
   - `schema_version` — a constant bumped whenever the review prompt/output format changes.
2. Storage: one file per entry under the reviewer's own dir, `STATE/<pr>/users/<login>/cache/<cache_key>.json`,
   written atomically (temp + rename, as Robin's state model already does). Store the full review
   result (findings, explainer, "what I checked/dropped," token usage) + `{created_at, source_run_id}`.
3. On trigger: compute `cache_key` before spending tokens. On hit, skip the LLM call and build a
   fresh run from the cached payload — the run's `run_id`, its select/edit/drop/post/approve state
   and its history entry are still created fresh and owned by that reviewer. Label it:
   "Reused your earlier run of this exact configuration on this commit — 0 tokens."
4. On miss: run normally, then write the result to the cache.
5. Bound the cache (oldest-first eviction past N entries or M days). Nice-to-have; entries are small
   and short-lived.
6. Token-usage chip on a cache hit: "0 tokens (reused your run from <time>)" — never hide it.

**Deferred (not in this phase): cross-reviewer cache sharing.** If a future decision wants Bob to
reuse Alice's identical-config generation, it may be added *only* with two hard guardrails:
(a) it must be visibly labelled as reused-from-Alice, and (b) **a shared/cached run must never count
toward the Phase-3 convergence signal** (it is one measurement, not two). Until that decision is
made explicitly, the cache stays per-user and this contradiction cannot arise.

**Acceptance criteria:** same reviewer, same PR head SHA, same effort/skill/model/focus → second
run completes near-instantly, 0 tokens, explicit "reused your run" label; change any one input →
full fresh run, no false hit; a *different* reviewer with the identical config still runs fresh
(no cross-user hit).

---

## Phase 2 — Nudge differentiation (create genuine independent units)

**Problem:** A second review only adds signal when it looks at something *different*. Two identical
reviews are correlated, not independent — they cannot honestly raise confidence (Phase 3).

**Design:** When the trigger screen (⌘K / paste-card / queue) detects another reviewer has already
completed a run on this PR's current head SHA, surface it inline before "Run":

> "Alice already reviewed this PR (Standard, no focus, team default skill). Add a focus note or
> switch skills to look at something different — or run the same way to compare notes."

Pull specifics (skill name, effort, focus) from run history — already stored. UI nudge only, never
enforcement: the reviewer may still run any config.

**Why it's load-bearing, not cosmetic:** differentiation is what *creates* the independent
configurations that Phase 3's confidence number is allowed to count. Without it, extra reviewers
mostly produce correlated observations.

**Acceptance criteria:** the second reviewer to open the trigger screen for a PR with a completed
run sees the prior run's config summarized, with one-click "focus on something else" (opens focus
field, prefocused) and "use a different skill" (opens skill picker).

---

## Phase 3 — Independence-weighted convergence (the actual "confidence" feature)

**Problem:** "More reviewers = more confidence" is assumed, not measured — and the naïve version of
measuring it (count how many runs raised a finding) is *pseudoreplication*: it treats correlated
runs as independent and inflates the number. This phase measures it honestly, and it is the single
most distinctive feature for the company pitch.

**Design — inter-rater agreement over a *set* of findings, weighted by independence.**

Two references map to two *different* jobs; keep them separate:
- *Self-consistency* (independent samples + diminishing returns, most gain by 3-5 genuinely
  independent samples) → justifies **why independence matters and why small N suffices**. Cut+Dry's
  typical 2-3 assigned reviewers sit right in that sweet spot.
- *Cohen's kappa / set-agreement* → the **aggregation mechanism**. A review is a *set of findings*,
  not one answer, so this is inter-rater agreement on sets, **not** self-consistency's
  majority-vote-to-one-answer. Do not fuse the two — fusing them silently turns "confidence" into
  "how many reviewers picked the same top finding," which is the wrong thing to measure.

1. **Finding fingerprint** for matching across independent runs on the same PR SHA:
   `{file_path, line_range (fuzzy-matched via Robin's existing diff-anchor logic), severity bucket,
   short semantic signature}`. Exact text won't match across skills/models; a *structural* match
   (same file, overlapping lines, same severity) is enough to start. **Do not** over-engineer into a
   semantic-similarity model. Treat matching as **noisy**: it will false-converge (same line,
   different concern) and false-diverge (same issue ±3 lines), so a v1 confirmation is a *candidate*
   that a reviewer can one-click confirm/reject ("these are the same finding") — that human confirm
   is what makes the signal trustworthy, and it also feeds Learnings.

2. **Independence weight (the pseudoreplication fix).** `n` for a finding is the number of
   *independent configurations* that raised it — **not** the number of runs. Compute a pairwise
   configuration-distance between the runs that agree (different skill? different model? different
   effort? different focus?). A confirmation across two *different* configs is close to independent
   (n≈2); across two *identical* configs — or a cache hit and its source — it is correlated (n≈1)
   and must not be counted as two.
   - **Convergent** (matched by 2+ *independent* configs) → tag "Confirmed by N independent reviews
     (<what differed>)", e.g. "Confirmed by 2 independent reviews (different skills)". Show it in the
     queue, the detail page, and — if the reviewer opts in — the posted GitHub comment body.
   - Matched only by near-identical configs → **downgrade or suppress**; do not present as a
     confirmation.
   - **Divergent** (raised by one config) → tag "Only flagged by <reviewer>'s <effort> run". This is
     *not* a demerit — it is the value of running more than one reviewer; call it out positively.

3. Store a small per-PR-SHA index, `STATE/<pr>/agreement/<sha>.json`, mapping fingerprints → the
   list of `{run_id, config}` that raised them, with the computed independence weight. Computed
   lazily when a new run completes on a SHA that already has prior runs — no background job.

4. **Agreement rate**, framed honestly: matched findings ÷ total distinct findings across
   *independent* runs on that SHA. Two cautions built in, not bolted on:
   - It is a **precision signal to improve toward**, never a marketing %. (Independent data shows
     AI-authored suggestions get adopted far less than human ones; read this number as
     human+Robin agreement, not proof.)
   - Because Phase 2 pushes reviewers to *diverge*, raw agreement can look *lower* exactly when the
     system is working as intended. Report it only over comparable/independent runs, and pair it
     with the divergence count so "low agreement" reads as "broad coverage", not "bad reviews".

**Acceptance criteria:** given two completed runs on the same PR SHA that used *different* configs,
with one overlapping finding and one unique finding each, the detail page shows the overlapping one
tagged "Confirmed by 2 independent reviews (different <skill|model|effort>)" and each unique one
tagged with which reviewer/run raised it; two runs that used the *same* config do **not** produce a
"confirmed" tag; the PR's agreement rate is computed over independent runs and retrievable via the
per-PR API the dashboard reads.

---

## Phase 4 — Trust dashboard for the pitch

**Problem:** you want to propose wider rollout off one week of pilot data, credible to skeptical
engineers, not just leadership.

**Design principle:** separate leading indicators (usage, keep-rate, agreement) from lagging ones
(cycle time, defect escape) so week-1 data isn't oversold. One new read-only page,
`/dashboard/rollup`, computed by aggregating files Robin already writes — no new instrumentation.

| Metric | Source | Type |
|---|---|---|
| Reviews run this week, by reviewer and PR | run history | leading / usage |
| Findings keep-rate (% posted as-is vs edited/dropped) | Learnings (`prbot_learn`) | leading / precision |
| Median "review requested" → first Robin-assisted comment posted | requested-at + post timestamps *(see note)* | lagging / cycle time |
| Tokens spent vs. estimated engineer-hours saved (runs × ~20 min baseline — **label as estimate**) | token-usage chip | leading / cost-ROI |
| Agreement rate across multi-reviewer PRs, **independence-weighted** (Phase 3) | agreement index | leading / confidence — **the differentiator** |
| 2-3 curated "Robin caught this" examples | posted findings | qualitative |

**Note / one real gap:** the cycle-time metric needs a clean **requested-at** timestamp.
`pr-watch` tracks per-`<pr>:<login>` seen-state but does not clearly persist a usable requested-at.
Before promising this metric, add a small write of `requested_at` when a reviewer is first seen on a
PR (one line in `pr-watch.sh` / the seen bookkeeping). Everything else on this page is pure
aggregation of existing files.

**Rollout ask (make it regardless of how good week-1 looks):** propose a staged 2-3 week expansion
to 2-3 more teams, not immediate company-wide. Controlled, reversible steps build more institutional
trust than a big-bang launch.

**Acceptance criteria:** the rollup renders for the past 7 days with no manual entry beyond the
curated anecdotes; every number is traceable to a file Robin writes (with `requested_at` added).

---

## Phase 5 — Team-skill audit trail (GitOps-on-save — small, but four named parts)

**Problem:** engineers distrust opaque AI output. Robin already counters this (Learnings, token
transparency, human-only post/approve). What's missing: the *review standard itself* has no audit
trail. The team-default skill is edited from the dashboard and lives in mutable box state
(`$ROOT/skills/_global.md`) — **not** version-controlled. (The installed `pr-review` skill is in
git; the editable team default is not.)

**Design — GitOps / config-as-code.** Keep `_global.md` as the working copy, but wire the
dashboard's save to *also* commit, so every change to "what Robin looks for" has real git history
(who, when, why). This is small and well-defined — but **four moving parts, not one line**:
1. **Which repo** the team skill commits to — a dedicated skills repo, or the robin checkout at
   `~/claude-pr-review-bot` (whose `$ROOT/skills/` is currently *not* a git checkout).
2. **A bot commit identity** (author/email) for dashboard-driven commits, plus the human editor's
   login in the commit message/trailer.
3. **Push credentials on the box** (a token/deploy key scoped to that one repo).
4. **Concurrent-edit story:** dashboard save is last-write-wins on a live file today; once it's git,
   two simultaneous edits need a pull-rebase-or-reject path, not a silent clobber.

Then surface the last N commits touching the skill file (via `git log` / `gh`) on the Skills page,
next to the keep/drop score, so any engineer can see how the standard evolved and that it's driven
by real usage data, not silently tuned.

**Acceptance criteria:** editing the team skill from the dashboard produces a git commit (bot
author, human editor named, message = the edit summary); the Skills page shows the current team
skill plus its last 5 revisions with author/date/message sourced from git history; two overlapping
edits do not silently overwrite each other.

---

## Build order

1. **Phase 2 (nudge)** — first. Nearly free (metadata already exists), aligned with per-reviewer
   independence, and it *creates the independent units* Phase 3 needs. Highest ratio of value to risk.
2. **Phase 1 (per-user re-run cache)** — self-contained, no UX/ philosophical risk once scoped
   per-user. Handles the double-click / re-run-same-config waste.
3. **Phase 3 (independence-weighted convergence)** — the standout for the pitch; needs Phase 2's
   differentiated runs to have real independent data to compare. Ship the noisy-match + human-confirm
   v1; do not chase semantic matching.
4. **Phase 4 (dashboard)** — pure aggregation once Phase 3 exists; do it right before the pitch.
   Add the `requested_at` write first.
5. **Phase 5 (skill audit trail)** — independent of the others; schedule it deliberately (four parts),
   don't call it free.

## Explicitly out of scope for now

- **Cross-reviewer cache sharing** — deferred; only with the two guardrails in Phase 1's "Deferred".
- **Semantic/fuzzy caching across different commits or PRs** — Robin's diffs are exact and small;
  content-addressed exact-match is sufficient.
- **A semantic-similarity model for finding-matching** — start structural + human-confirm.
- **No new database, no background jobs** — every phase uses the file-based per-PR/per-user state.
- **No enforcing differentiation** — Phase 2 is a nudge, never a rule; the point is genuine choice.
