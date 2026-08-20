# dex-engine — the shared engine behind every dex instance

Public repo (github.com/leeovery/dex). It contains machinery only — never
personal data, tokens, or instance content, and no knowledge of any specific
instance: instances are private content repos that run this machinery, and
the engine stays unaware of which ones exist.

## Structure

- `src/dex_engine/` — the mechanical commands, exposed as entry points in
  `pyproject.toml` (`dex-normalize`, `dex-enrich`, `dex-lint`, `dex-exclude`,
  `dex-inbox`, `dex-sync`, `dex-render`, `dex-new`) and run in instances
  through the `bin/dex` shim (`uvx --from` this repo at the pinned tag):
  - `pipeline/` — the ledger-driven core: `types.py` (enums, dataclasses,
    Instance/Config), `ledger.py` (the one serialization boundary),
    `detect.py`, `registry.py` (explicit ordered driver list), `run.py`
    (the orchestrator; the pipeline's single broad except), `classify.py`
    (central failure classification + the scrubber), `transcribe.py`,
    `urls.py`, `capture.py` (`enrich item new`), `issues.py` (the
    issue filer).
  - `drivers/` — one per source shape: youtube, x, github, paper, podcast,
    web (catch-all, always last), file; `transport.py` is the HTTP seam.
  - `capabilities/` — transcribe (whisper-local floor, whisper-api),
    extract (anydoc, csv-builtin, cognitive floor), ocr (cognitive floor).
  - `render/` — `kernel.py` (pure layout), `surfaces.py` (named report
    surfaces), `cli.py` (`dex-render`). Judgment decides, code renders.
  - `migrations/` — numbered state migrations, run by sync before anything
    touches state.
  - `corpus.py` — the ONE corpus-item frontmatter read/write point.
  - `enrich.py` · `normalize.py` · `inbox.py` · `lint.py` · `sync.py` ·
    `exclude.py` · `new.py` — thin argparse CLIs over injected
    Instance/Config; zero import-time state anywhere.
- `instance/` — the template for a new instance, bundled into the wheel:
  the `dex` shim, `gitattributes`, `dex-contract.md` (the shared instance
  contract, synced to `.claude/dex-contract.md` and imported by every
  instance's CLAUDE.md), and `skills/` (`dex-capture`, `dex-run` with its
  `references/`, `dex-query`, `dex-lint` — each dir synced recursively into
  `.claude/skills/`; the `dex-` skill namespace is engine-owned and sync
  retires skills the template drops). `CLAUDE.md` and `README.md` are
  scaffold seeds — written by `dex-new` at creation, instance-owned
  afterwards (they hold only identity, scope, and instance-specifics;
  everything shared lives in the synced contract).
- `docs/` — human guides only: `start.md` (the single entry point, fetched
  raw by the getting-started prompt: dependencies, create or join, schedule,
  capture), `shortcut.md` (build the phone shortcut), and `capture.md` (the
  capture protocol any client implements).
- `design/ingestion-pipeline.md` — the agreed, in-force design for the
  ingestion machinery; `design/roadmap.md` — queued design work,
  operational facts, pending tasks.
- `example/` — a toy instance showing the shapes.

## Design (current, in force)

- **One capture method.** Every capture is one `.md` in the instance's
  `inbox/`; the note lives in the file body. Binaries stage as assets on a
  standing `inbox` release and `dex-inbox` materializes them into
  `media/<id>/` where LFS applies, deleting the asset. Git history stays
  text-only. In-session captures go through the dex-capture skill — same
  file, committed directly. Protocol: `docs/capture.md`.
- **Mechanical vs cognitive.** Engine commands fetch, move, and verify
  deterministically; the ledger is the work queue and the run report is the
  session's work list. Understanding — describing media, digesting,
  placing, writing wiki pages — is Claude's job, specified in the synced
  skills. Reports render through surfaces, never hand-drawn.
- **No server-side machinery.** No Actions, no webhooks: the contents-API PUT
  is the commit, and failures surface in sessions where someone can act.
- **Auth is borrowed, never stored.** Commands resolve GITHUB_TOKEN, then
  `gh auth token`. Declared instance dependencies: git, git-lfs, uv, gh
  (authed). The only stored credential in the whole system is the PAT inside
  the phone shortcut.
- **Engine/instance separation.** Instances hold content plus synced
  machinery; every machinery change happens here and reaches instances via
  `bin/dex sync` (tag-pinned: `.dex-engine-pin` names the release; Mint
  cuts releases, sync bumps pins and runs migrations). Nothing is ever
  fixed by hand-editing a synced file in an instance.

## When you change things (anti-drift rules)

- **The ingestion design is written down**: `design/ingestion-pipeline.md`
  is the agreed design in force for the pipeline, state, releases, and
  skills. Check changes against it; when a decision genuinely changes,
  record the new decision (roadmap for queued work) rather than silently
  diverging from the document.
- **Comments earn their place — and never reference the design doc.** A
  comment or docstring exists only for what the code cannot say: a
  non-obvious constraint, a deliberate surprise, a wild-data fact, a why.
  No narration, no restating the line below, no traceability breadcrumbs
  ("§14", "phase-3 review", "per the design"). When a constraint from the
  design matters at a call site, inline it in full, self-contained — the
  design doc never ships to instances, so a pointer to it is broken on
  arrival, and even in-repo it rots. This file and the roadmap are the only
  places that may name the design doc.
- **Commands** (add / rename / change behavior): update the `pyproject.toml`
  entry points, the `instance/dex` usage line, and the README command table
  together.
- **Capture format or flow**: `docs/capture.md`, `docs/shortcut.md`,
  `instance/skills/dex-capture/`, `instance/skills/dex-run/` (SKILL.md +
  references), and `src/dex_engine/inbox.py` describe one design — change
  them together.
- **Corpus or state file shapes**: `instance/skills/dex-run/references/`
  (`schema.md`, `state-formats.md`) is the contract; change it in the same
  commit as the code and skills that read those files — and ship a
  migration when existing state must move.
- **Anything under `instance/`**: after pushing and releasing, run
  `bin/dex sync` in every instance you maintain and commit there.
- **Decisions**: record in `design/roadmap.md` — present tense, current design
  only; this file and the docs describe what IS, not what was.
- **README**: its under-the-hood section mirrors the structure and design
  above — keep it true whenever any of this changes.
- **`docs/shortcut.md` is device-verified**: never guess iOS Shortcuts UI —
  verify on a device or ask the owner what their screen shows.
