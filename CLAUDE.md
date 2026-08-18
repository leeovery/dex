# dex-engine — the shared engine behind every dex instance

Public repo (github.com/leeovery/dex). It contains machinery only — never
personal data, tokens, or instance content, and no knowledge of any specific
instance: instances are private content repos that run this machinery, and
the engine stays unaware of which ones exist.

## Structure

- `src/dex_engine/` — the mechanical commands (`dex-normalize`, `dex-enrich`,
  `dex-lint`, `dex-exclude`, `dex-inbox`, `dex-sync`), exposed as entry points
  in `pyproject.toml` and run in instances through the `bin/dex` shim
  (`uvx --from` this repo).
- `templates/` — what instances get: the `dex` shim, `gitattributes`,
  `dex-contract.md` (the shared instance contract, synced to
  `.claude/dex-contract.md` and imported by every instance's CLAUDE.md), and
  `skills/` (each skill dir synced recursively into `.claude/skills/`,
  including `references/`). `CLAUDE.md` and `instance-README.md` are scaffold
  seeds — copied at instance creation, instance-owned afterwards (they hold
  only identity, scope, and instance-specifics; everything shared lives in
  the synced contract).
- `docs/` — human guides only: `shortcut.md` (build the phone shortcut),
  `capture.md` (the capture protocol any client implements), `backfill.md`
  (bulk imports).
- `design/roadmap.md` — queued design work, operational facts, pending tasks.
- `example/` — a toy instance showing the shapes.
- `.claude/skills/new-dex-instance/` — the scaffolding skill; runs here, never
  synced to instances.

## Design (current, in force)

- **One capture method.** Every capture is one `.md` in the instance's
  `inbox/`; the note lives in the file body. Binaries stage as assets on a
  standing `inbox` release and `dex-inbox` materializes them into
  `media/<id>/` where LFS applies, deleting the asset. Git history stays
  text-only. Protocol: `docs/capture.md`.
- **Mechanical vs cognitive.** Engine commands fetch, move, and verify
  deterministically. Understanding — describing media, digesting, placing,
  writing wiki pages — is Claude's job, specified in the synced skills.
- **No server-side machinery.** No Actions, no webhooks: the contents-API PUT
  is the commit, and failures surface in sessions where someone can act.
- **Auth is borrowed, never stored.** Commands resolve GITHUB_TOKEN, then
  `gh auth token`. Declared instance dependencies: git, git-lfs, uv, gh
  (authed). The only stored credential in the whole system is the PAT inside
  the phone shortcut.
- **Engine/instance separation.** Instances hold content plus synced
  machinery; every machinery change happens here and reaches instances via
  `bin/dex sync`. Nothing is ever fixed by hand-editing a synced file in an
  instance.

## When you change things (anti-drift rules)

- **Commands** (add / rename / change behavior): update the `pyproject.toml`
  entry points, the `templates/dex` usage line, and the README command table
  together.
- **Capture format or flow**: `docs/capture.md`, `docs/shortcut.md`,
  `templates/skills/dex-ingest/` (SKILL.md + references), and
  `src/dex_engine/inbox.py` describe one design — change them together.
- **Corpus or state file shapes**: `templates/skills/dex-ingest/references/`
  (`schema.md`, `state-formats.md`) is the contract; change it in the same
  commit as the code and skills that read those files.
- **Anything under `templates/`**: after pushing, run `bin/dex sync` in every
  instance you maintain and commit there.
- **Decisions**: record in `design/roadmap.md` — present tense, current design
  only; this file and the docs describe what IS, not what was.
- **README**: its under-the-hood section mirrors the structure and design
  above — keep it true whenever any of this changes.
- **`docs/shortcut.md` is device-verified**: never guess iOS Shortcuts UI —
  verify on a device or ask the owner what their screen shows.
