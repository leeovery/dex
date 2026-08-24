# Start here

This guide sets up a dex — a git-backed knowledge base that Claude operates
for its owner. You do ALL the work; the owner never runs commands. Report
every blocker loudly and never work around a failure silently.

---

## Step 1: New or Existing?

Ask the owner: "Are we creating a new dex, or setting up an existing one on
this machine?" — unless the request that brought you here already says. An
instance README's join prompt names an existing dex and its repo; never
re-ask what the owner already stated.

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

From `{home}`: `gh repo clone {repo}`, then inside the clone
`git lfs install --local` and `git lfs pull` — on a machine that never ran
`git lfs install` globally, the clone checks LFS media out as pointer
files, and the pull materializes them. If the clone is denied, the owner's
GitHub account lacks access to `{repo}` — resolve that with them before
anything else.

Set `{instance}` = the clone's absolute path and `{name}` = its directory
name. Bring its machinery current — from `{instance}`: `bin/dex sync`
(commit and push if it changed anything), then `bin/dex inbox`
(materializes any staged binary captures and checks the standing inbox
release). Follow the inbox output: anything it materialized is committed
and pushed immediately, for the reason it states.

→ Proceed to **Step 5**.

#### If `{mode}` is `new`

Interview first. Ask all five questions together, in one round — not one
at a time (use your ask-user tool where available):

1. **Name** — suggest `dex-<domain>` (dex-cooking, dex-travel). Keep the
   dex brand; the domain is the qualifier. Set `{name}` = the answer.
2. **What it covers** — 2-4 bullets. Anything not covered is out of scope
   by default; at processing time the agent asks the owner about borderline
   items rather than guessing.
3. **GitHub** — private repo (default), or local-only for now. Local-only
   is a supported state, not a half-setup: runs skip pulls, pushes and the
   inbox release checks (skipped, not passed) until a repo exists, and the
   phone shortcut and second machines need the repo.
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
  `<owner>/<repo>` placeholder in its "Run it on another machine" prompt —
  that prompt is what a second machine or a second person pastes, so it has
  to name the real repo. If GitHub was declined, delete the section.
- Commit — from `{instance}`, like every command from here on. If GitHub
  was wanted: `gh repo create {name} --private --source . --push`, then
  `bin/dex inbox ensure` — creates the standing "inbox" release that
  binary captures stage into.
- Sanity check: `bin/dex lint` (prints a fresh-instance notice).

→ Proceed to **Step 5**.

---

## Step 5: Schedule It

Ask the owner how often this instance should run, with a recommendation:
hourly for an instance receiving captures daily; once a day for a quiet
one. Then arm the schedule: a **local scheduled task** in the Claude
desktop app (Code tab → Routines → New routine → Local), which runs the
instance's dex-run skill on the owner's machine with their full
environment. The task:

- Name `{name}-run`; the chosen frequency; instructions exactly:
  "Work in the folder {instance}.
  This is a scheduled, unattended run. Read .claude/skills/dex-run/SKILL.md
  and perform its Every run section exactly. Never ask the owner
  questions; report what was done."
  The first line is load-bearing: task creation from a session cannot set
  the task's folder (no folder parameter), so the prompt must carry it.
- **Model**: recommend the owner set the task's model to **Opus-class or
  above** — the run's core work is judgment (scope, harvest, digests, wiki
  synthesis), and a lighter model degrades it invisibly. Skills cannot pin
  a model; the scheduled task can. dex is not Claude-exclusive (any agent
  runtime that reads skills could drive it) but is untested elsewhere —
  say so rather than implying portability is verified.
- In a desktop session that can create scheduled tasks, create it
  yourself; otherwise hand the owner those exact values for the
  New-routine form (Local; worktree off; permission mode Auto).
- The created task's Folder field will show the session's folder, not the
  instance. Walk the owner through fixing it: open the task → Edit → the
  Folder field sits below the prompt → set it to `{instance}` → save.
- Then have the owner Run now once, answering permission prompts with
  "always allow", so scheduled firings never stall on approval.
- Local tasks fire only while the app is open and the machine awake; a
  missed schedule catches up once on wake. Captures wait in the inbox
  meanwhile — nothing is lost.
- An owner who doesn't use the desktop app has no local task to arm. The
  trigger is theirs then — a cron or launchd entry that starts a session
  in the instance, or nothing at all, since asking for a run does the
  same work. Record what they chose and move on: an unscheduled instance
  is a supported state, not a failed setup. Then do the first run
  yourself, in this session — read
  `{instance}/.claude/skills/dex-run/SKILL.md` and perform its Every run
  section — so setup still ends with the instance current.

---

## Step 6: Phone Capture

Skip this step if the owner declined the shortcut, or the instance has no
GitHub repo. Otherwise fetch the shortcut guide — it lives in the engine
repo, which is never cloned, so fetch it raw:
https://raw.githubusercontent.com/leeovery/dex/main/docs/shortcut.md

Each person captures with their own token. Walk them through, concretely:

1. The token, per the guide's **Before you begin** section: fine-grained,
   scoped to ONLY this instance repo, permissions: Contents R/W.
2. Install the shortcut from the iCloud link at the top of the guide — on
   import it asks for their instances and token.
3. Using it: share anything from any app and pick **Send To Dex**. If it
   isn't in the share sheet, scroll to the very bottom, tap **Edit
   Actions**, add Send To Dex, and drag it to the top so it's always in
   reach.

---

## Step 7: Backfill

#### If the owner has exports to ingest

Exports go in `{instance}/raw/`; then follow the Backfills section of the
instance's dex-run skill (`.claude/skills/dex-run/SKILL.md`).

---

## Step 8: Hand-Off

End your final message with, in a few lines: `{instance}` (where it
lives), its scope as written, the schedule it runs on, how to save things
(the shortcut, or "add this to dex" in a session — either way the save is
instant and processing happens on the schedule), and how to ask questions.
Health checks run themselves on the schedule. The owner never touches the
machinery.
