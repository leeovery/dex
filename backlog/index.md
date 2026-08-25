# dex — backlog

Every idea not yet in hand lives here: features and bug fixes alike, one
file per idea. This index is a bare list, one line per idea; the substance
lives only in the idea's own file, never here.

The convention:

- One markdown file per idea, kebab-case filename, added to this list when
  the file is created.
- The file opens with `# <name>` and a first paragraph that says what the
  idea is; the rest is whatever the idea has earned so far (facts, options
  mapped, constraints, when to pick it up).
- An idea graduates by being designed: write `design/<name>.md` (a
  fleshed-out design, kept for reference after it ships), then delete the
  backlog file and its line here. Nothing is ever "done" in the backlog.
- Dropping an idea is the same motion without the design: delete the file
  and the line, and say why in the commit message.

## Ideas

- [Watchers — feeding sources as a first-class layer](watchers.md) — every feeder becomes a watcher writing standard inbox captures; the next thing to design
- [Self-tuning cadence](self-tuning-cadence.md) — scheduled runs reschedule their own task: hourly while captures flow, daily when quiet
- [Ingestion pipeline — open ends](ingestion-open-ends.md) — X thread walk-down and engine OCR providers, deferred by the ingestion design
- [Instagram driver](instagram-driver.md) — shape not agreed; no official API, options mapped
- [Hosted transcription providers](hosted-transcription.md) — Groq, Fireworks, Deepgram, Workers AI: a documented recommended config
- [Per-instance tokens in the phone shortcut](shortcut-per-instance-tokens.md) — one token field serves every instance today; cross-org sets cannot work
- [Per-instance context instructions](per-instance-context.md) — standing content context beyond the scope list, machinery identical
- [Source removal](source-removal.md) — no story yet for removing an entire source after ingestion
- [Resurfacing and the owner's reading queue](resurfacing-reading-queue.md) — read/intent flags and views so saved things stop vanishing from mind
- [App-only owner surfaces](app-only-owners.md) — the owner who lives in the Claude app, no computer
- [Offline instances can't run bin/dex at all](offline-instances.md) — uvx resolves the engine from the network every invocation
- [Paid media/object storage](paid-media-storage.md) — S3 or R2 instead of LFS for media at scale
- [Pre-rewrite dead ledger entries need healing](dead-entry-healing.md) — per-instance content work during each post-merge sync review

## Reference

- [Operational facts](operational-facts.md) — device-verified environment facts the ideas above lean on (shortcut mechanics, scheduled-task hosts, cowork limits)
