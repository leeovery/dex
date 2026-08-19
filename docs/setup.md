# Set up this machine to run a dex instance

This guide sets up a dex knowledge-base instance for its owner. The owner's
prompt names the instance repo as `owner/repo`. Work in the folder the
session has access to; report every blocker loudly and never work around a
failure silently.

## 1. Tools

Check for `git`, `git-lfs`, `uv`, and `gh`. For any that are missing, ask the
owner before installing (`brew install git git-lfs uv gh` on macOS). If there
is no way to install, stop and report exactly what is missing.

## 2. GitHub auth

Run `gh auth status`. If not logged in, run `gh auth login` and walk the
owner through it (it opens a browser). Their GitHub account must have access
to the instance repo — if the clone in the next step is denied, that is the
first thing to check.

## 3. Clone

From the session's folder: `gh repo clone owner/repo`, then `git lfs install
--local` inside the clone. Tell the owner the absolute path of the clone —
it is their knowledge base's home on this machine.

## 4. Bring the instance current

From the clone: `bin/dex sync` (refreshes the instance's machinery from the
engine; commit and push if it changed anything), then `bin/dex inbox`
(reconciles any waiting captures and verifies the capture staging release).

## 5. Start running it

Read `.claude/skills/dex-run/SKILL.md` in the clone and follow it. Its first
run asks the owner how often the instance should run and establishes the
schedule; after that the instance ingests, health-checks, and maintains
itself.

## 6. Phone capture

If the owner wants one-tap capture from their phone, point them at
`docs/shortcut.md` in the engine repo (github.com/leeovery/dex) — the
ready-made shortcut link is pinned at the top. Their capture token must have
access to this instance's repo.
