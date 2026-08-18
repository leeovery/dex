---
name: dex-new-volume
description: Scaffold a new dex knowledge-base volume — a content repo that runs dex-engine as a dependency. Use when the user wants to create a new knowledge base, brain, or dex volume (e.g. "new dex for marketing", "set up a knowledge base for X").
---

# Create a new dex volume

A volume is a (typically private) repo holding one domain's knowledge: corpus,
enrichment, wiki, state. The engine (this repo) stays a dependency — never copy
its code into the volume.

## 1. Interview the user (briefly)

- **Domain + name**: volumes are conventionally `dex-<domain>` (dex-marketing,
  dex-cooking). Keep the dex brand; the domain is a qualifier.
- **Scope**: what's IN and what's OUT, 2-4 bullets each. Out-scope matters as much
  as in — it drives the ingest filter forever.
- **Visibility/owner**: default private, under the user's account.
- **Sources**: chat exports to backfill? inbox-only? This decides whether raw/ is
  needed now.

## 2. Scaffold

From an empty dir (or `gh repo create <name> --private --clone`):

```bash
mkdir -p corpus enrichment wiki/topics wiki/entities wiki/syntheses state/digests raw bin .github/workflows
curl -sL https://raw.githubusercontent.com/leeovery/dex-engine/main/templates/CLAUDE.md -o CLAUDE.md
curl -sL https://raw.githubusercontent.com/leeovery/dex-engine/main/templates/kb -o bin/kb && chmod +x bin/kb
curl -sL https://raw.githubusercontent.com/leeovery/dex-engine/main/templates/inbox-caller.yml -o .github/workflows/inbox.yml
printf '.DS_Store\n.env\n' > .gitignore
printf '# Inbox — capture suggestions\n\nOne line per suggestion; triaged at ingest; lines removed once handled.\n' > state/inbox.md
printf '{\n  "name_map": {},\n  "internal_domains": [],\n  "noise_prefixes": []\n}\n' > state/normalize-config.json
printf '# Index\n\nNo pages yet — first ingest pending.\n' > wiki/index.md
printf '# Ops log\n' > wiki/log.md
printf '# Pins\n\nHuman corrections as claim+anchor; regeneration must re-apply these.\n' > wiki/pins.md
```

## 3. Fill in CLAUDE.md

Replace the `<volume>`, `<Domain>`, and Scope placeholders with the interview
answers. The Scope section is the volume's constitution — write it precisely.

## 4. Capture setup (tell the user, they act)

1. Fine-grained PAT: repository access = this volume only; permissions =
   Contents R/W + Issues R/W.
2. Capture client: any POST to `https://api.github.com/repos/<owner>/<volume>/issues`
   with headers `Authorization: Bearer <PAT>`, `Accept: application/vnd.github+json`
   and JSON body `{"title": <url>, "body": <optional note>}`. On iOS: a 3-action
   Shortcut (Receive from Share Sheet → Ask for Input → Get Contents of URL).
   WARNING: never share the Shortcut publicly or pin its iCloud link in a public
   repo — shared shortcuts embed the PAT.
3. Optionally set repo notifications to Ignore (the inbox workflow's issue
   open/close traffic is noise).

## 5. First operations

- Backfill: exports into `raw/`, then `bin/kb normalize` → scope-filter pass →
  `bin/kb enrich run` → digest each item → build taxonomy → assemble wiki pages.
  For large backfills (500+ items), work in waves and verify coverage mechanically
  between waves; write work manifests BEFORE dispatching parallel agents and
  include "if an input is missing, STOP — do not improvise" in agent contracts.
- Steady state: drain `state/inbox.md` per the ingest operation in CLAUDE.md;
  run `bin/kb lint` periodically and fix what it flags.

Commit and push when scaffolded. Report the repo URL, the scope as written, and
the capture-client instructions back to the user.
