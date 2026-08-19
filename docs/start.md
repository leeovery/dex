# Start here

This guide sets up a dex — a git-backed knowledge base that Claude operates
for its owner. You do ALL the work; the owner never runs commands. Report
every blocker loudly and never work around a failure silently.

---

## Step 1: New or Existing?

Ask the owner: "Are we creating a new dex, or setting up an existing one on
this machine?"

Set `{mode}` = `new` or `existing`.

#### If `{mode}` is `existing`

Ask: "What's the GitHub URL for your dex instance?" Accept a URL,
`owner/repo`, or just a name. Set `{repo}` = the `owner/repo` worked out
from whatever they give.

---

## Step 2: Dependencies

Check for `git`, `git-lfs`, `uv`, and `gh`. For any that are missing, ask
the owner before installing (`brew install git git-lfs uv gh` on macOS). If
there is no way to install, stop and report exactly what is missing.

Run `gh auth status`. If not logged in, run `gh auth login` and walk the
owner through it (it opens a browser). Their GitHub account must have access
to `{repo}` (existing) or be able to create a repo (new).

---

## Step 3: A Home

Ask where the dex should live, suggesting `~/Code/dex` — one home for all
their dex instances, co-located. Set `{home}` = the answer; create the
directory if it doesn't exist. If the session can't
write to `{home}`, report it and ask the owner to grant access rather than
choosing somewhere else silently.

---

## Step 4: Get the Instance

#### If `{mode}` is `existing`

From `{home}`: `gh repo clone {repo}`, then `git lfs install --local`
inside the clone. If the clone is denied, the owner's GitHub account lacks
access to `{repo}` — resolve that with them before anything else.

Set `{instance}` = the clone's absolute path. Bring its machinery current:
`bin/dex sync` (commit and push if it changed anything), then `bin/dex
inbox` (reconciles waiting captures and verifies the capture staging
release).

→ Proceed to **Step 5**.

#### If `{mode}` is `new`

Interview first. Ask all five questions together, in one round — not one
at a time (use your ask-user tool where available):

1. **Name** — suggest `dex-<domain>` (dex-cooking, dex-travel). Keep the
   dex brand; the domain is the qualifier. Set `{name}` = the answer.
2. **What it covers** — 2-4 bullets. Anything not covered is out of scope
   by default; at ingest time the agent asks the owner about borderline
   items rather than guessing.
3. **GitHub** — private repo (default), or local-only for now.
4. **Capture** — do they want the one-tap phone shortcut? (Needs a GitHub
   repo and a token they create — exact steps come in Step 6.)
5. **Backfill** — any existing exports (chat exports, bookmarks dump) to
   ingest, or starting fresh from capture?

Set `{instance}` = `{home}/{name}`. Scaffold it — from `{home}`:

```bash
uvx --from git+https://github.com/leeovery/dex dex-new {name}
```

This builds the whole instance from the engine's template: directory tree,
seed CLAUDE.md and README.md, machinery, git init, local LFS. Then:

- Fill CLAUDE.md: `{name}`, the domain, and the In-scope list exactly as
  interviewed.
- Personalize README.md: owner/domain, the same In-scope list (the two
  files mirror each other; scope changes always update both), and the
  `owner/repo` in its "Run it on another machine" prompt.
- Commit. If GitHub was wanted: `gh repo create {name} --private --source .
  --push`, then `bin/dex inbox ensure` — creates the standing "inbox"
  release that binary captures stage into.
- Sanity check: `bin/dex lint` from `{instance}` (prints a fresh-instance
  notice).

→ Proceed to **Step 5**.

---

## Step 5: Schedule It

Read `{instance}/.claude/skills/dex-run/SKILL.md` and follow its first run.
It asks the owner how often the instance should run, then schedules in
place when the host supports recurring runs (Claude Cowork does) — or, when
it can't (Claude Code), runs once and hands the owner the one-line Cowork
instruction that arms the schedule.

---

## Step 6: Phone Capture

Each person captures with their own token. Walk them through, concretely:

1. GitHub → Settings → Developer settings → Fine-grained tokens → new token
   scoped to ONLY this instance repo, permissions: Contents R/W.
2. Install the shortcut from the link pinned at the top of
   `docs/shortcut.md` — on import it asks for their instances and token.
3. Using it: share anything from any app and pick **Send To Dex**. If it
   isn't in the share sheet, scroll to the very bottom, tap **Edit
   Actions**, add Send To Dex, and drag it to the top so it's always in
   reach.

---

## Step 7: Backfill

#### If the owner has exports to ingest

Exports go in `{instance}/raw/`; then follow the Backfills section of the
instance's dex-ingest skill (`.claude/skills/dex-ingest/SKILL.md`).

---

## Step 8: Hand-Off

End your final message with, in a few lines: `{instance}` (where it
lives), its scope as written, the schedule it runs on, how to save things
(the shortcut, or "add this to dex" in a session), and how to ask
questions. Health checks run themselves on the schedule. The owner never
touches the machinery.
