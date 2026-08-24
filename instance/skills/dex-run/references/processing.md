# Processing

Items, then the enrich run, then the backstop — in that order. The
per-item cognitive work every step dispatches is `ingest-item.md` (this
directory).

1. **Items.** For each capture file in `inbox/`: the scope check, then
   `bin/dex enrich item new` — the per-item reference prescribes both.
   Item creation is mechanical and fast; it must precede the enrich run
   so the pipeline seeds the new items' URLs.

2. **Enrich.** `bin/dex enrich run`, and **read its report — it is the
   work list**. It is markdown, and its sections say who owns the next
   action. Complete the per-item cognitive work for *everything* under
   **Needs writing up** — fresh captures, drained reruns, rewritten
   items, newly drained waiting cohorts, and no-source items (text or
   image captures — nothing to fetch, everything to describe and digest)
   — plus everything under **Read these yourself**. Items you created
   this session always get the full per-item procedure whether or not the
   report names them. **Needs you** holds the entries the engine has
   given up on, each with a stated reason: judge them per the reference's
   heal procedure. **Waiting on the engine** is the engine's own retry
   queue — read it, act on nothing.

3. **Backstop.** `bin/dex enrich status` — any item listed under **Digest
   these** is an interrupted previous session: complete its remaining
   per-item steps now, harvest → digest → place → wiki. The listing says
   a digest is owed, not that harvest ran — the interruption can predate
   either step, and no surface here would catch a skipped harvest before
   the next health check. Every item listed is digestible; one still
   owing a unit is `raw` and never appears there, however long it stays
   parked. **Needs you** and **Waiting on the engine** here are the
   standing view of everything parked in the instance, not just this
   session's: judge the **Needs you** entries per the reference's heal
   procedure exactly as on the run report, and act on nothing under
   **Waiting on the engine**.
