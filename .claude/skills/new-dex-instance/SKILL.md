---
name: new-dex-instance
description: Set up a new dex knowledge base (a "instance") end-to-end — interview the user, scaffold the repo, configure capture. Use when the user wants a new knowledge base, brain, or dex instance ("set up a dex for marketing", "make a knowledge base for my business").
---

# Set up a new dex instance

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

First check the machine has the tools an instance depends on: `git`, `git-lfs`,
`uv`, and `gh` (authed — `gh auth status`). If any is missing, ask before
installing it (`brew install gh git-lfs` etc.); binary capture and ingest need
gh's auth.

```bash
mkdir -p <path> && cd <path> && git init
mkdir -p corpus enrichment wiki/topics wiki/entities wiki/syntheses state/digests raw bin media
# templates from the engine repo (you may be running inside a clone of it — copy locally if so)
curl -sL https://raw.githubusercontent.com/leeovery/dex/main/templates/CLAUDE.md -o CLAUDE.md
curl -sL https://raw.githubusercontent.com/leeovery/dex/main/templates/instance-README.md -o README.md  # then personalize
curl -sL https://raw.githubusercontent.com/leeovery/dex/main/templates/dex -o bin/dex && chmod +x bin/dex
bin/dex sync   # pulls engine-managed machinery: dex-ingest/query/lint skills, .gitattributes
printf '.DS_Store\n.env\n' > .gitignore
mkdir -p inbox && touch inbox/.gitkeep
git lfs install --local   # media/ and image captures are LFS-tracked
printf '{\n  "name_map": {},\n  "internal_domains": [],\n  "noise_prefixes": []\n}\n' > state/normalize-config.json
printf '# Index\n\nNo pages yet — first ingest pending.\n' > wiki/index.md
printf '# Ops log\n' > wiki/log.md
printf '# Pins\n\nHuman corrections as claim+anchor; regeneration must re-apply these.\n' > wiki/pins.md
```

Then:
- Fill CLAUDE.md: instance name, domain, and the In-scope list exactly as interviewed.
- Personalize README.md: owner/domain AND the same In-scope list — the two files
  mirror each other; scope changes always update both.
- Commit. If GitHub was wanted: `gh repo create <name> --private --source . --push`,
  then `bin/dex inbox ensure` — creates the standing "inbox" release that binary
  captures stage into.
  - Sanity check: `bin/dex lint` from the instance root (prints a fresh-instance notice).

## 3. Capture setup (only their PAT needs their hands)

Walk them through, concretely:
1. GitHub → Settings → Developer settings → Fine-grained tokens → new token scoped
   to ONLY this instance repo, permissions: Contents R/W.
2. iOS Shortcut: walk them through `docs/shortcut.md` (the universal
   recipe — dictionary of instances, auto-skips the picker when there's only one).
3. Tell them plainly: never share the Shortcut or its iCloud link publicly — shared
   shortcuts embed the PAT.

## 4. Backfill (if any)

Exports go in `raw/`, then run the pipeline: `bin/dex normalize` → scope-filter pass
(agent judgment, exclusions via `bin/dex exclude`) → `bin/dex enrich run` → digest every
item → taxonomy → assemble wiki pages → verify. For large backfills (500+ items):
work in waves; verify coverage mechanically between waves; write work manifests
BEFORE dispatching parallel agents; every agent contract includes "if an input is
missing, STOP — do not improvise".

## 5. Hand-off note (end your final message with this)

Tell the user, in a few lines: where the instance lives, its scope as written, how to
save things (shortcut + "add this to dex" in a session), how to ask questions, and
that "run a lint" is the periodic health check. They never touch the machinery.
