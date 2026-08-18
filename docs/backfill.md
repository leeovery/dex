# Backfills — bulk-importing existing history

For chat exports, bookmark dumps, and other bulk sources — typically once per
source when an instance is born. Day-to-day material arrives via capture
(`docs/capture.md`) instead.

## Getting the exports

- **Discord** — [DiscordChatExporter](https://github.com/Tyrrrz/DiscordChatExporter):
  export the channel(s) as **JSON**. Works with a user token for servers you're
  a member of.
- **Google Chat** — [Google Takeout](https://takeout.google.com): select only
  Google Chat, export, and take the per-space JSON out of the archive.

`dex-normalize` understands both formats natively. For any other source,
either convert it to one of these shapes or feed items through the capture
protocol / a session instead — don't teach the normalizer one-off formats.

## Process

1. Drop the export files in `raw/` — verbatim, append-only; they are the
   permanent record of what was received.
2. Fill `state/normalize-config.json` (shapes: the ingest skill's
   `references/state-formats.md`): `name_map` for sharer display names,
   `internal_domains` and `noise_prefixes` for what to drop.
3. `bin/dex normalize` — one corpus item per share, provenance stamped.
   Re-runnable; fix normalizer issues by re-running, never by hand-editing
   corpus items.
4. **Scope-filter pass** (judgment): read the items against the instance's
   In-scope list; purge out-of-scope ones with `bin/dex exclude <file.json>` —
   exclusions survive re-normalization.
5. `bin/dex enrich run` — fetch everything behind every URL. Caption-less
   video/audio: `bin/dex enrich whisper` (OPENAI_API_KEY in the instance
   `.env`).
6. Digest, taxonomy, and wiki assembly — Claude's work, per the ingest skill.

## At scale (500+ items)

- Work in waves with a mechanical coverage check between each (every item
  digested? every digest placed?) — never trust "the agents said done".
- Write work manifests BEFORE dispatching any parallel agents.
- Every agent contract includes: "if an input is missing, STOP — do not
  improvise".
