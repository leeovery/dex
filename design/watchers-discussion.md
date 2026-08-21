# Watchers — preliminary discussion (NOT a design)

Status: **discussion log, 2026-08-20**. This is not an agreed design — it
captures a conversation so nothing is lost. Positions below may contradict
each other; that's deliberate and unresolved. Pick this up **after the
ingestion-pipeline rewrite lands** (`design/ingestion-pipeline.md`), recap,
and design it properly then.

Relation to the pipeline design: this sits entirely **upstream** of it. The
interface between the two systems is the capture protocol (one `.md` in
`inbox/`); the pipeline design needs zero changes to accommodate any of
this, which was checked deliberately. Same discussion transcript as the
pipeline design (held locally by the owner, as noted at the top of that
document).

---

## The core idea (the owner's framing)

Separate **feeding items in** from **processing them**. The phone shortcut
is not special — it's just the first mechanism we built for putting
captures in the inbox. Discord is not special either — it's just another
mechanism of feeding items in, and it currently gets a bespoke parallel
path (raw/ exports + normalize) it shouldn't need. Copy-pasting a message
into a session and saying "queue this" is the same act. All of them should
converge on one artifact — the inbox capture file — and one downstream
process.

Consequences the owner named:

- There could be many feeders, attached as required, at setup or any time:
  a Discord channel, an Instagram saved folder ("Dex Engineering"), an X
  bookmarks folder, a Dropbox folder. (Those are examples, not a list —
  source ideas are part of the future design.)
- A **watching system**: sources that are watched for inputs and simply add
  items to the inbox. Processing then happens on schedule or on demand,
  completely in line with every other item.
- "We normalise and make as much the same as possible."

## The provisional shape (Claude's sharpening — parts contested below)

```
                    PRODUCERS                      THE INTERFACE          CONSUMER

  push (human      phone shortcut ────────┐
  intent)          dex-capture in session ┤
                                          ├────▶   inbox/ capture     ▶  the pipeline
  pull             discord watcher ───────┤        files (one shape)      (unchanged)
  (watchers)       rss / dropbox watcher ─┤
                   x-bookmarks watcher ───┘
```

- Same Protocol pattern as drivers/capabilities, one layer up:

  ```python
  class Watcher(Protocol):
      name: str
      def available(self) -> Availability: ...
      def poll(self, cursor: Cursor) -> tuple[list[Capture], Cursor]: ...
  ```

- Engine ships watcher implementations (machinery, identical everywhere);
  instances **attach** them with per-instance params (which channels, which
  folder) in config, creds in `.env`.
- Watchers run inside the dex-run session, right after sync/pull: poll,
  write captures, then inbox materialize and everything downstream proceeds
  as designed. No second schedule. **(Owner: agreed.)**
- Staleness becomes honest machinery instead of magic: a configured watcher
  owns its source, has a cursor and an `available()`, and reports its own
  state ("discord: unavailable (no exporter), cursor 2d old"). This
  replaces the vaguer "backfill-source freshness" roadmap idea.
- CONTESTED (see rulings): Claude initially framed ongoing-Discord-via-
  watcher as demoting `normalize` to a "bulk history tool for initial
  backfill".

## The owner's refinements and rulings (this discussion)

1. **A watcher is not "now-forward". Discord is a watcher, not history.**
   "Watch this place — this box, this channel — and import anything you
   see, using configuration to control scope." The cursor position is the
   scope control: cursor at 0 = drain the entire available history first,
   then keep watching; cursor at now = today-forward. **Same process either
   way** — one starts at the beginning and loops until caught up; the other
   starts at the current moment. Setup asks: "Pull in history? How far
   back? Or from today forward?" Do not shape Discord as backfill-only.
2. **`raw/` should probably die.** It's really a temporary space. If it
   survives at all it becomes namespaced watcher scratch —
   `cache/imports/<watcher>/` or similar — where a watcher stages files
   *temporarily* while feeding them, one item at a time, into the inbox.
   The system never "pulls from raw"; only the watcher touches its own
   scratch space.
3. **One standardized inbox inserter.** A single shared code path for
   writing a correctly-shaped capture file into `inbox/`, used by every
   watcher — and probably by the dex-capture skill too. Everything feeds
   through it; it's the choke point that guarantees the one-shape claim.
4. **Availability is only checked for *configured* watchers.** An
   unconfigured watcher is not "unavailable" — it's unattached. Separately,
   the system needs a way to make attachable watchers **discoverable**
   (what could I attach?). How, is an open question.
5. **Naming is open.** "Importer" suggests bulk; "watcher" suggests
   new-only; the owner leans **watcher** with the corrected meaning from
   (1), but wants the naming discussed. (Candidates floated for later:
   watcher, source, feed, tap, collector.)

## Claude's positions logged for the future discussion

- Push vs pull may not deserve different names — a "push watcher" is just
  one whose poll is a no-op because humans deliver to it. Or push producers
  are simply "capture clients" and watchers are strictly pull. Unresolved.
- The feasibility ladder (indicative only): RSS and folder-watch trivial
  and high-value; Discord medium (exporter/token questions relocate into
  one watcher's `available()`, which is the right place for them — user
  token automation is against Discord ToS, a constraint that shapes
  cadence); X bookmarks has a real API but paid-tier; Instagram saved
  folders have no official API — but note the folder-watch idea reframes
  the parked Instagram problem from enrichment-side to capture-side, which
  may be the easier half.
- Immediate Discord handling (pre-watchers): do nothing clever — manual
  refresh, staleness visible; the watcher design makes it properly
  automatic later.

## Open questions (all unresolved)

1. **Naming** — watcher vs something else; and whether push producers
   (shortcut, dex-capture) share the name or stay "capture clients".
2. **What happens to `normalize` and existing `raw/` exports.** If ongoing
   Discord flows watcher→inbox, does the message-grouping/threading logic
   normalize owns (grouping messages into items, threaded-reply folding)
   move into the Discord watcher? And the existing `raw/discord/` archives
   in instance repos: delete (capture-preserved-in-git-history principle),
   or keep as archive? (Overlaps the source-removal roadmap item.)
3. **Capture protocol extension** — watchers need provenance fields
   (`source`, `channel`, `shared_by`) in capture frontmatter; the shortcut
   just doesn't set them. Touches `docs/capture.md`, which third-party
   clients implement.
4. **History-drain volume.** Cursor-at-zero on a busy channel produces
   hundreds of captures in one poll — an inbox flood. Does the per-item
   capture flow absorb that (waves, caps), or does history-drain need a
   bulk mode? The backfill wave-discipline in the ingest skill exists for
   exactly this shape of problem.
5. **Cursor state** — where it lives (`state/` file per watcher vs one
   file), and its merge semantics: a cursor is last-write-wins scalar
   state, not append-only — JSONL union-merge is wrong for it. Multi-
   machine instances make this a real question.
6. **Discovery and attachment UX** — how an owner learns what's attachable
   (`dex watchers` listing? part of the capability report?), and what
   attach/detach looks like (setup-time and any-time).
7. **Cross-watcher dedup** — the same URL arrives via X bookmarks and a
   Discord share. Downstream ledger hash-dedup already absorbs the fetch;
   is capture-level dedup wanted, or is two-arrivals signal? (Claude leans:
   signal — the schema already treats duplicate URLs from different
   sharers as legitimate.)
8. **What counts as a capture inside a watched source** — a Discord channel
   has chatter; "messages with links or attachments" is a mechanical
   filter, but text-only gems exist. Mechanical filter in the watcher,
   judgment at scope-check — does that line hold against the real
   channels?
9. **Watcher config shape** — per-watcher configuration (channels, folder
   names, history depth, poll scope) in `state/config.json` vs per-watcher
   files; creds stay in `.env`.
10. **Rate limits and politeness** for history drains (a cursor-at-zero
    drain hammering an API), and whether the pipeline's SLEEP-per-driver
    idea has a watcher-side analogue.

## Explicitly not decided

Everything above. This document exists so the next session starts from the
full discussion instead of memory. Recap it, resolve the contradictions
(especially normalize/raw's fate and the naming), answer the open
questions, then write the actual design.
