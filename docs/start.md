# Start here

This guide sets up a dex — a git-backed knowledge base that Claude operates
for its owner. You do ALL the work; the owner never runs commands. Report
every blocker loudly and never work around a failure silently.

## 1. New or existing?

Ask the owner: "Are we creating a new dex, or setting up an existing one on
this machine?" For an existing one, ask: "What's the GitHub URL for your dex
instance?" — accept a URL, `owner/repo`, or just a name, and work out the
repo from whatever they give.

## 2. Dependencies

Check for `git`, `git-lfs`, `uv`, and `gh`. For any that are missing, ask
the owner before installing (`brew install git git-lfs uv gh` on macOS). If
there is no way to install, stop and report exactly what is missing.

Run `gh auth status`. If not logged in, run `gh auth login` and walk the
owner through it (it opens a browser). Their GitHub account must have access
to the instance repo (existing) or be able to create one (new).

## 3. A home

Ask where the dex should live, suggesting `~/Code` (create it if it doesn't
exist). If the session can't write to the chosen location, report it and ask
the owner to grant access rather than choosing somewhere else silently.

## 4. Get the instance

**Existing** — clone it: `gh repo clone owner/repo` into the chosen folder,
then `git lfs install --local` inside the clone. If the clone is denied, the
owner's GitHub account lacks access to the repo — resolve that with them
before anything else. Then bring the machinery current: `bin/dex sync`
(commit and push if it changed anything) and `bin/dex inbox` (reconciles any
waiting captures and verifies the capture staging release).

**New** — interview first (one round, not twenty questions; use your
ask-user tool where available):

1. **Name** — suggest `dex-<domain>` (dex-cooking, dex-travel). Keep the dex
   brand; the domain is the qualifier.
2. **What it covers** — 2-4 bullets. Anything not covered is out of scope by
   default; at ingest time the agent asks the owner about borderline items
   rather than guessing.
3. **GitHub** — private repo (default), or local-only for now.
4. **Capture** — do they want the one-tap phone shortcut? (Needs a GitHub
   repo and a token they create — exact steps come at the end.)
5. **Backfill** — any existing exports (chat exports, bookmarks dump) to
   ingest, or starting fresh from capture?

Then build, silently competent:

```bash
mkdir -p ~/Code/<name> && cd ~/Code/<name> && git init   # under the home chosen in step 3
mkdir -p corpus enrichment wiki/topics wiki/entities wiki/syntheses state/digests raw bin media
curl -sL https://raw.githubusercontent.com/leeovery/dex/main/templates/CLAUDE.md -o CLAUDE.md
curl -sL https://raw.githubusercontent.com/leeovery/dex/main/templates/instance-README.md -o README.md
curl -sL https://raw.githubusercontent.com/leeovery/dex/main/templates/dex -o bin/dex && chmod +x bin/dex
bin/dex sync   # pulls engine-managed machinery: skills, contract, .gitattributes
printf '.DS_Store\n.env\n' > .gitignore
mkdir -p inbox && touch inbox/.gitkeep
git lfs install --local
printf '{\n  "name_map": {},\n  "internal_domains": []\n}\n' > state/normalize-config.json
printf '# Index\n\nNo pages yet — first ingest pending.\n' > wiki/index.md
printf '# Ops log\n' > wiki/log.md
printf '# Pins\n\nHuman corrections as claim+anchor; regeneration must re-apply these.\n' > wiki/pins.md
```

Then:

- Fill CLAUDE.md: instance name, domain, and the In-scope list exactly as
  interviewed.
- Personalize README.md: owner/domain, the same In-scope list (the two files
  mirror each other; scope changes always update both), and the `owner/repo`
  in its "Run it on another machine" prompt.
- Commit. If GitHub was wanted: `gh repo create <name> --private --source .
  --push`, then `bin/dex inbox ensure` — creates the standing "inbox" release
  that binary captures stage into.
- Sanity check: `bin/dex lint` from the instance root (prints a
  fresh-instance notice).

## 5. Schedule it

Read `.claude/skills/dex-run/SKILL.md` in the instance and follow its first
run. It asks the owner how often the instance should run, then schedules in
place when the host supports recurring runs (Claude Cowork does) — or, when
it can't (Claude Code), runs once and hands the owner the one-line Cowork
instruction that arms the schedule.

## 6. Phone capture (each person's own token)

Walk them through, concretely:

1. GitHub → Settings → Developer settings → Fine-grained tokens → new token
   scoped to ONLY this instance repo, permissions: Contents R/W.
2. iOS Shortcut: install from the ready-made link pinned at the top of
   `docs/shortcut.md`; the import questions take their instances and token.
3. Tell them plainly: their configured copy embeds their PAT — sharing it is
   safe only because the import questions clear those fields; never remove
   the import questions from a copy that gets shared.

## 7. Backfill (if any)

Exports go in `raw/`; then follow the Backfills section of the instance's
dex-ingest skill (`.claude/skills/dex-ingest/SKILL.md`).

## 8. Hand-off note (end your final message with this)

Tell the owner, in a few lines: where the instance lives, its scope as
written, the schedule it runs on, how to save things (shortcut + "add this
to dex" in a session), and how to ask questions. Health checks run
themselves on the schedule. They never touch the machinery.
