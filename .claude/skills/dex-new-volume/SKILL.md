---
name: dex-new-volume
description: Set up a new dex knowledge base (a "volume") end-to-end — interview the user, scaffold the repo, configure capture. Use when the user wants a new knowledge base, brain, or dex volume ("set up a dex for marketing", "make a knowledge base for my business").
---

# Set up a new dex volume

You do ALL the work — the user never runs commands. Interview, then build, then hand
them a short "how to use it" note.

## 1. Interview (use AskUserQuestion where available; one round, not twenty questions)

1. **Name** — suggest `dex-<domain>` (dex-marketing, dex-cooking). Keep the dex
   brand; the domain is the qualifier.
2. **What it covers** — 2-4 bullets. Anything not covered is out of scope by
   default; at ingest time the agent asks the owner about borderline items rather
   than guessing.
3. **Where it lives** — local path (default `~/Code/<name>`), and GitHub: private
   repo (default), or local-only for now.
4. **Capture** — do they want the one-tap phone shortcut? (Requires a GitHub repo
   and a fine-grained PAT they create — you provide exact steps at the end.)
5. **Backfill** — any existing exports (Discord, Google Chat, bookmarks dump) to
   ingest, or starting fresh from capture?

## 2. Build (you do this, silently competent)

```bash
mkdir -p <path> && cd <path> && git init
mkdir -p corpus enrichment wiki/topics wiki/entities wiki/syntheses state/digests raw bin .github/workflows
# templates from the engine repo (you may be running inside a clone of it — copy locally if so)
curl -sL https://raw.githubusercontent.com/leeovery/dex/main/templates/CLAUDE.md -o CLAUDE.md
curl -sL https://raw.githubusercontent.com/leeovery/dex/main/templates/volume-README.md -o README.md  # then personalize
curl -sL https://raw.githubusercontent.com/leeovery/dex/main/templates/kb -o bin/kb && chmod +x bin/kb
bin/kb sync   # pulls engine-managed machinery: dex-ingest/query/lint skills, inbox workflow
printf '.DS_Store\n.env\n' > .gitignore
printf '# Inbox — capture suggestions\n\nOne line per suggestion; triaged at ingest; lines removed once handled.\n' > state/inbox.md
printf '{\n  "name_map": {},\n  "internal_domains": [],\n  "noise_prefixes": []\n}\n' > state/normalize-config.json
printf '# Index\n\nNo pages yet — first ingest pending.\n' > wiki/index.md
printf '# Ops log\n' > wiki/log.md
printf '# Pins\n\nHuman corrections as claim+anchor; regeneration must re-apply these.\n' > wiki/pins.md
```

Then:
- Fill CLAUDE.md: volume name, domain, and the In-scope list exactly as interviewed.
- Commit. If GitHub was wanted: `gh repo create <name> --private --source . --push`,
  and offer to set repo notifications to Ignore (inbox issue traffic is noise):
  `gh api -X PUT /repos/<owner>/<name>/subscription -F ignored=true`.
- Sanity check: `bin/kb lint` from the volume root (prints a fresh-volume notice).

## 3. Capture setup (only their PAT needs their hands)

Walk them through, concretely:
1. GitHub → Settings → Developer settings → Fine-grained tokens → new token scoped
   to ONLY this volume repo, permissions: Contents R/W + Issues R/W.
2. iOS Shortcut (3 actions): Receive from Share Sheet (URLs/Text) → Ask for Input
   (Text, "Why? (optional)") → Get Contents of URL:
   POST `https://api.github.com/repos/<owner>/<volume>/issues`,
   headers `Authorization: Bearer <PAT>` + `Accept: application/vnd.github+json`,
   Request Body JSON with `title` = Shortcut Input, `body` = Provided Input.
3. Tell them plainly: never share the Shortcut or its iCloud link publicly — shared
   shortcuts embed the PAT.

## 4. Backfill (if any)

Exports go in `raw/`, then run the pipeline: `bin/kb normalize` → scope-filter pass
(agent judgment, exclusions via `bin/kb exclude`) → `bin/kb enrich run` → digest every
item → taxonomy → assemble wiki pages → verify. For large backfills (500+ items):
work in waves; verify coverage mechanically between waves; write work manifests
BEFORE dispatching parallel agents; every agent contract includes "if an input is
missing, STOP — do not improvise".

## 5. Hand-off note (end your final message with this)

Tell the user, in a few lines: where the volume lives, its scope as written, how to
save things (shortcut + "add this to dex" in a session), how to ask questions, and
that "run a lint" is the periodic health check. They never touch the machinery.
