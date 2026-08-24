# First-run setup

Ask the owner how often this instance should run, with a recommendation:
hourly for an instance receiving captures daily; once a day for a quiet one.
Then arm the schedule: a **local scheduled task** in the Claude desktop app
(Code tab → Routines → New routine → Local), which runs this skill on the
owner's machine with their full environment. The task:

- Name `<instance>-run`; the chosen frequency; instructions exactly:
  "Work in the folder <absolute path to this instance>.
  This is a scheduled, unattended run. Read .claude/skills/dex-run/SKILL.md
  and perform its Every-run procedure exactly. Never ask the owner
  questions; report what was done."
  The first line is load-bearing: task creation from a session cannot set
  the task's folder (no folder parameter), so the prompt must carry it.
- **Model**: recommend the owner set the task's model to **Opus-class or
  above** — the run's core work is judgment (scope, harvest, digests, wiki
  synthesis), and a lighter model degrades it invisibly. Skills cannot pin
  a model; the scheduled task can. dex is not Claude-exclusive (any agent
  runtime that reads skills could drive it) but is untested elsewhere —
  say so rather than implying portability is verified.
- In a desktop session that can create scheduled tasks, create it yourself;
  otherwise hand the owner those exact values for the New-routine form
  (Local; worktree off; permission mode Auto).
- The created task's Folder field will show the session's folder, not the
  instance. Walk the owner through fixing it: open the task → Edit → the
  Folder field sits below the prompt → set it to the instance → save.
- Then have the owner Run now once, answering permission prompts with
  "always allow", so scheduled firings never stall.
- Local tasks fire only while the app is open and the machine awake; a
  missed schedule catches up once on wake. Captures wait in the inbox
  meanwhile — nothing is lost.
- An owner who doesn't use the desktop app has no local task to arm. The
  trigger is theirs then — a cron or launchd entry that starts a session in
  this instance, or nothing at all, since asking for a run does the same
  work. Record what they chose and move on: an unscheduled instance is a
  supported state, not a failed setup.
