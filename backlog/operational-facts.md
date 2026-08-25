# Operational facts

Device-verified environment facts the backlog ideas lean on
(shortcut mechanics, scheduled-task hosts, cowork limits). Not an idea:
reference only.

- Shared iCloud shortcut links are frozen snapshots: edits never propagate;
  each re-share mints a new URL; importers must reinstall and re-answer import
  questions. Fields with import questions are auto-cleared in the shared copy.
- Shortcuts' Base64 Encode must have Line Breaks = None (GitHub rejects
  wrapped base64).
- GitHub returns 404-shaped JSON for repos a token can't reach — Shortcuts
  shows a checkmark; silent failure.
- Instance repos owned by orgs can't use no-expiration fine-grained PATs;
  classic PAT (repo scope) is the workaround.
- Shortcuts variable chips can go stale after nearby edits: the chip renders
  normally but resolves empty at run time. Symptom: "Cannot parse response"
  on a request whose config looks correct. Fix: delete and re-insert the
  chips.
- Get Images from Input dereferences URL inputs (fetches them); the X app's
  share URL redirects through a non-HTTPS scheme and errors. Links must be
  detected and diverted before any Get Images call.
- Desktop scheduled tasks (Claude app, Code tab → Routines → Local) run as
  real local Claude Code sessions: full user PATH, gh auth, git-lfs, uv,
  unrestricted network. File-backed at
  `~/.claude/scheduled-tasks/<name>/SKILL.md` (frontmatter + prompt body;
  schedule/folder/model live outside the file). Presets
  manual/hourly/daily/weekdays/weekly; finer cron by asking a desktop
  session. Fire only while the app is open and the machine awake; one
  catch-up run on wake covers the most recent miss (7-day window).
- Cowork scheduled tasks are cloud unless a local folder is declared at
  creation (can't be added later). "Run on your computer" there means an
  Ubuntu aarch64 VM with the folder mounted at the identical path: egress
  proxy allows github.com only (not raw.githubusercontent.com or any other
  subdomain), git/uv/ffmpeg/python present, no gh, no git-lfs, no root.
  Git over HTTPS to github.com works; enrichment fetches and the
  release-asset API are blocked.
- Cowork cloud containers have full network and git/git-lfs/uv/ffmpeg —
  the engine builds and runs there via uvx — but no gh; auth would need a
  stored per-instance PAT.
