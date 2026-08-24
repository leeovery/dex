# Preparation

Make the instance current before any other work touches it. Five steps,
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
   - **Commit the refreshed files and the pin** — this session owns that
     commit step; sync itself never commits. Message: `sync: engine <tag>`
     (or `sync: machinery refresh` when unpinned).

3. **Guard.** The dirty-tree check runs *after* sync's commit,
   deliberately: sync legitimately edits state (pin bump, migration
   rewrites and seeds) before any guard could pass, so a guard placed
   earlier would trip on its own machinery. After the sync commit, a
   still-dirty tree means a previous session died mid-work — stop and
   report; do not build on its state.

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

   - `state/*.jsonl` merge as a union automatically. Verify no conflict
     markers remain in them; if any do, the union driver did not run,
     and the resolution is still the union: keep both sides' lines,
     markers removed.
   - `state/taxonomy.json` and `state/entity-members.json` are the two
     files sessions write directly, so merge them by judgment: the
     union of both sides' topics, entities and member lists,
     deduplicated.
   - A conflicted `state/digests/<id>.md` is re-derived, never
     hand-spliced: the verb is the writer, so read both sides, write
     the merged judgment as the payload, and run `bin/dex enrich item
     digest --file cache/digest.json`.
   - `wiki/*` is a build artifact: rewrite each conflicted page whole
     from the merged state (digests and taxonomy), rather than splicing
     around markers.

   Then commit the resolution with a message that says what it merged,
   and continue the run.

5. **Inbox.** `bin/dex inbox` — materializes staged binary captures. It
   needs GitHub auth (gh logged in, or GITHUB_TOKEN); if it reports
   missing auth or any FAIL line, stop and report — in an attended
   session, fix it with the owner before continuing; never work around it
   by hand-downloading. Follow its output: when it materialized anything,
   commit and push immediately, for the reason it states (the release
   assets are deleted; the repo copy is the only copy until pushed). If it
   reports orphaned assets, re-run after a minute before raising them with
   the owner (a capture may still be in flight).
