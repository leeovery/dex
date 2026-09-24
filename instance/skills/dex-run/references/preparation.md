# Preparation

Make the instance current before any other work touches it. Six steps,
in order:

1. **Anchor.** Confirm the working directory is this instance's root (the
   folder containing this `.claude/`); change to it first if it isn't, and
   stay inside it for the whole session. Never enter another dex instance —
   if siblings exist nearby, ignore them; they might as well not exist.

2. **Sync first.** `bin/dex sync` — before anything else touches state,
   because sync bumps the engine pin and runs migrations, and a session on
   stale code writes stale vocabulary into state that newer code then
   rejects. Then:
   - **Review the sync report.** Any migration `skipped`/`anomalies` are
     repaired with judgment now, before other work builds on them — the
     code declined what it could not do safely and said so; do not ignore
     it. Dropped ledger lines are not repair work: a migration drops only
     lines nothing on disk or in the corpus can own, the report names them,
     and git history holds the pre-migration ledger.
   - **Carry a Lens note line to the owner.** The line is informational:
     `lens.md` still holds the seed's placeholder lines or cannot be read,
     so this dex reads as general knowledge until the owner fills in or
     deletes the file. The run goes on, never edits `lens.md` for the
     note, and carries it into the closing report (dex-run step 6). When
     the report carries no such line, `lens.md` either states a lens or is
     missing or empty, and an instance with no lens is a general knowledge
     dex, which is a legitimate way to run one.
   - **Commit the refreshed files and the pin** — this session owns that
     commit step; sync itself never commits. Message: `sync: engine <tag>`
     (or `sync: machinery refresh` when unpinned).

3. **Guard.** The dirty-tree check runs *after* sync's commit,
   deliberately: sync legitimately edits state (pin bump, migration
   rewrites and seeds) before any guard could pass, so a guard placed
   earlier would trip on its own machinery. After the sync commit, a
   still-dirty tree means a previous session died mid-work. Never build
   on it silently; clear it deliberately, now, or every later run trips
   here on the same residue:

   - **Inspect the residue** (`git status`, `git diff`) and attribute
     it. Engine-written residue is coherent work a dead session never
     committed: corpus items, enrichment files, state appends, deleted
     capture files. Commit it with a marked message (e.g. `recovered:
     previous run died mid-work`) and proceed — the run's redrain
     re-seeds and retries whatever was half-done, so nothing recovered
     this way is trusted as finished.
   - **A directive's unfinished work** is never recovered: a dead
     session's edits to the files a pending directive names (`bin/dex
     directive list` still names it, and `bin/dex directive show <n>` says
     which files) are half a judgment nobody checked. When that is all
     the tree holds, restore it to HEAD exactly as step 5 does when `done`
     refuses, and step 5 performs the directive again from the start.
   - **A tree left mid-merge** (conflict markers, an unfinished merge in
     git's own state) is the pull procedure's case arriving early:
     resolve it per step 4's file classes, commit, and continue.
   - **Residue you cannot attribute stays put**, uncommitted and
     untouched, and IS the loud report: stop and describe exactly what
     is sitting in the tree. Recovery is a deliberate act with a commit
     that says so, never silent building-on-top.

4. **Pull.** `git pull` — captures arrive as commits, and other
   machines' commits arrive with them. A local-only instance (no origin
   remote) has nothing to pull: skip the step — skipped, not passed, the
   same reading `bin/dex inbox` gives its release checks — and skip the
   run's later push steps the same way.

   **A conflicted pull is resolved in this session, never left.** Two
   machines that both ran since they last synced can conflict, and a
   tree left mid-merge trips the next run's guard forever, so finish
   the merge before any other work touches the instance. Resolution is
   judgment, which is exactly what a session is for: an unattended run
   performs it the same way, and never stops to ask. Per file class:

   - `state/*.jsonl` and `state/exclusions.tsv` merge as a union
     automatically — an exclusion is never lost in a merge. Verify no
     conflict markers remain in them; if any do, the union driver did
     not run, and the resolution is still the union: keep both sides'
     lines, markers removed.
   - A conflicted `state/taxonomy.json` or `state/entity-members.json`
     merges by judgment — the union of both sides' topics, entities and
     member lists — but the verb is the writer: keep either side whole,
     then move the other side's missing definitions and memberships in
     through a `bin/dex enrich place` payload.
   - A conflicted `state/digests/<id>.md` is re-derived, never
     hand-spliced: the verb is the writer, so read both sides, write
     the merged judgment as the payload, and run `bin/dex enrich item
     digest --file cache/digest.json`.
   - `wiki/*` is a build artifact: rewrite each conflicted page whole
     from the merged state (digests and taxonomy), rather than splicing
     around markers — except `wiki/index.md`, which is rendered:
     `bin/dex map` recompiles it.
   - A conflicted corpus file is two machines' engine refreshes
     deriving different `status`/`enrichment:` fields. The body is
     verbatim forever and identical on both sides; keep either side's
     frontmatter, and the next run's refresh re-derives it from disk.
   - Anything else that conflicts is resolved the same way in spirit:
     understand what each side added, merge by judgment, and never
     leave a marker in a committed file.

   Then commit the resolution with a message that says what it merged,
   and continue the run.

5. **Directives.** `bin/dex directive list` names the directives the
   engine ships that this instance has not completed, in the order they
   run. It reads `state/directives.jsonl` as the pull left it, so a
   directive another machine already performed is not listed again: act on
   this list, not on the one in the sync report. A directive is the
   engine's decision about this instance's own files, written for a run
   with nobody present, so never ask the owner anything about one and
   never wait for an answer. For each pending directive, in numeric order:

   - Run `bin/dex directive show <n>` and perform its instructions exactly.
     Every file they name is yours to edit, `state/config.json` included.
   - Run `bin/dex directive done <n>`. It runs the directive's check and
     appends its record to `state/directives.jsonl` only when every
     condition holds.
   - Commit the directive's edits and that record together as one commit,
     `directive <n>: <intent>`. The run's normal push carries it.

   **When `done` refuses**, nothing is recorded and the next run performs
   the directive again, so leave nothing of this attempt behind: restore
   the working tree to HEAD with `git reset --hard HEAD`, then remove the
   files the attempt created with `git clean -fd` (the guard left the tree
   clean, so every untracked file is the attempt's own, and ignored files
   such as `cache/` and `.env` are untouched). Residue left in the tree
   trips the next run's guard. Treat instructions you cannot carry out the
   same way, without running `done`. Then perform no further directive this
   run, since a later one may build on the one that failed. A directive the
   engine shipped that cannot complete is an engine defect: file it with
   `bin/dex issue` per the "Engine defects" rubric in `processing.md` (this
   directory), with `"verb": "directive"`, `"expected": "directive <n>
   completes and passes its own check"` and `"observed": "directive <n> did
   not complete on this instance"`, worded exactly so, because the same
   wording on every later run is what dedups the report to one issue. Put
   what the check reported, in abstract terms, in `steps`, and the concrete
   detail in `note`. Then continue the run with the inbox.

6. **Inbox.** `bin/dex inbox` — materializes staged binary captures. It
   needs GitHub auth (gh logged in, or GITHUB_TOKEN); if it reports
   missing auth or any FAIL line, stop and report — in an attended
   session, fix it with the owner before continuing; never work around it
   by hand-downloading. Follow its output: when it materialized anything,
   commit and push immediately, for the reason it states (the release
   assets are deleted; the repo copy is the only copy until pushed). If it
   reports orphaned assets, re-run after a minute before raising them with
   the owner (a capture may still be in flight).
