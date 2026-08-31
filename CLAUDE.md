# dex-engine — the shared engine behind every dex instance

Public repo (github.com/leeovery/dex). It contains machinery only — never
personal data, tokens, or instance content, and no knowledge of any specific
instance: instances are private content repos that run this machinery, and
the engine stays unaware of which ones exist.

## Structure

- `src/dex_engine/` — the mechanical commands, exposed as entry points in
  `pyproject.toml` (`dex-normalize`, `dex-enrich`, `dex-lint`, `dex-exclude`,
  `dex-inbox`, `dex-sync`, `dex-render`, `dex-new`, `dex-issue`,
  `dex-serve`, `dex-connect`) and run in instances through the `bin/dex` shim
  (`uvx --from` this repo at the pinned tag):
  - `pipeline/` — the ledger-driven core: `types.py` (enums, dataclasses,
    Instance/Config), `ledger.py` (the one serialization boundary),
    `detect.py`, `registry.py` (explicit ordered driver list), `run.py`
    (the orchestrator; the pipeline's single broad except), `classify.py`
    (central failure classification + the scrubber), `transcribe.py`,
    `enrichment.py` (the one enrichment-file format point), `urls.py`,
    `ownership.py` (which live item claims a work unit), `capture.py`
    (`enrich item new`), `digest.py` (`enrich item digest` — the ONE
    digest write point), `issues.py` (the issue filer), `observed.py`
    (`dex issue` — the observation-shaped producer and its leak
    rejectors).
  - `drivers/` — one per source shape: youtube, x, instagram, github,
    paper, podcast, web (catch-all, always last), file; `transport.py` is
    the HTTP seam.
  - `capabilities/` — transcribe (whisper-local floor, whisper-api),
    extract (anydoc, csv-builtin, cognitive floor), ocr (cognitive floor).
  - `render/` — `kernel.py` (markdown composition primitives),
    `surfaces.py` (named report surfaces), `cli.py` (`dex-render`).
    Judgment decides, code renders. `design/ingestion-pipeline.md` §11
    records where the surface vocabulary and layout rules came from;
    `surfaces.py` is what they are now.
  - `serve/` — the MCP server (`dex-serve`): `roster.py` (which instances
    one process serves, and the `<instance>/<item-id>` namespace),
    `library.py` (the reads: search, fetch, page), `steering.py` (the
    words: instructions, footers, the dex-query prompt read from the
    bundled skill), `server.py` (the tool and resource wiring), `cli.py`,
    and `connect.py` (`dex-connect` — the one merge into a chat client's
    own config, launching through an anchor instance's shim so no second
    version pin exists). Hands, not an agent — it never calls a model and
    never judges relevance; the calling model does the searching.
  - `migrations/` — numbered state migrations, run by sync before anything
    touches state.
  - `corpus.py` — the ONE corpus-item frontmatter read/write point.
  - `template.py` — the ONE place that knows where the wheel-bundled
    `instance/` tree lives (sync, `dex-new`, the server's prompt).
  - `enrich.py` · `normalize.py` · `inbox.py` · `lint.py` · `sync.py` ·
    `exclude.py` · `new.py` · `issue.py` — thin argparse CLIs over injected
    Instance/Config; zero import-time state anywhere.
- `instance/` — the template for a new instance, bundled into the wheel:
  the `dex` shim, `gitattributes`, `dex-contract.md` (the shared instance
  contract, synced to `.claude/dex-contract.md` and imported by every
  instance's CLAUDE.md), and `skills/` (`dex-capture`, `dex-query`, and
  `dex-run` + `dex-lint` each with their `references/` — each dir synced
  recursively into `.claude/skills/`; the `dex-` skill namespace is
  engine-owned and sync retires skills the template drops). `CLAUDE.md` and `README.md` are
  scaffold seeds — written by `dex-new` at creation, instance-owned
  afterwards (they hold only identity, scope, and instance-specifics;
  everything shared lives in the synced contract).
- `docs/` — human guides only: `start.md` (the single entry point, fetched
  raw by the getting-started prompt: dependencies, create or join, schedule,
  capture), `shortcut.md` (build the phone shortcut), `capture.md` (the
  capture protocol any client implements), and `connect.md` (fetched raw the
  same way: wire this machine's instances into the desktop app's chat).
- `design/` — one markdown file per fleshed-out design, written when a
  backlog idea has been discussed into a shape worth building. **A design
  file is a locked snapshot, not a living document.** It records what was
  decided and why, at the moment it was decided; it is not a claim about
  how the system works today. The code moves on, and so do the owner's
  views. Read one to understand why something is the way it is — never as
  the authority on what is currently true. That authority is the code, this
  file, and the docs. Updating a shipped design is allowed but unusual, and
  never the default reflex.
- `backlog/` — one file per idea not yet in hand (features and bug fixes
  alike), indexed in `backlog/index.md`, which also states the convention.
  An idea graduates by getting its `design/<name>.md`.
- `field-notes.md` — hard-earned, device-verified facts about the platforms
  dex builds on (Shortcuts, GitHub tokens, scheduled-task hosts, cowork).
  Agent-facing, never owner-facing. Check it before building on any of
  those surfaces; append what you verify the hard way.
- `.github/workflows/` — `live.yml` alone, the daily third-party drift
  watch. The gates run on your machine and at the release tag, never in
  Actions. See Development below.
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
  skills. State-bearing reports render through surfaces, never
  hand-drawn; the one hand-composed report is a session's closing
  summary to the owner — free-form by design, its required content
  fixed by the dex-run skill.
- **No server-side machinery in an instance.** An instance runs no Actions and
  no webhooks: the contents-API PUT is the commit, and failures surface in
  sessions where someone can act. This is a rule about the *data* path — no
  robot writes to a knowledge base. The engine repo is a code repo and does run
  one Action, the drift watch (below); nothing in it touches an instance.
- **Auth is borrowed, never stored.** Commands resolve GITHUB_TOKEN, then
  `gh auth token`. Declared instance dependencies: git, git-lfs, uv, gh
  (authed). The only stored credential in the whole system is the PAT inside
  the phone shortcut.
- **Engine/instance separation.** Instances hold content plus synced
  machinery; every machinery change happens here and reaches instances via
  `bin/dex sync` (tag-pinned: `.dex-engine-pin` names the release; Mint
  cuts releases, sync bumps pins and runs migrations). Nothing is ever
  fixed by hand-editing a synced file in an instance.

## Development

**The gates.** Four commands, run in two places and nowhere else — on your
machine as you work, and in front of the release tag:

```
uv run pytest -m "not live"     # the default suite (`uv run pytest` is the same:
                                #  -m 'not live' is in pyproject's addopts)
uv run ruff check
uv run ty check
uv run ruff format --check .
```

**Formatting gates because the tree is format-clean.** `ruff format` reaches
only shipped code: pyproject's `extend-exclude` keeps it away from markdown
(the design doc's aligned protocol sketches are prose, not code) and the
vendored `.agents/`. Within that boundary every file satisfies the formatter,
and the check exists to keep it that way — format what you touch, and never
reformat files you are not otherwise editing.

**The shim is tested under four shells.** "POSIX sh" is not one interpreter —
macOS's `/bin/sh` is bash in POSIX mode, a Linux instance's is dash — so
`tests/test_shim.py` parametrizes every case over `sh`, `bash`, `dash` and
`ksh`, skipping any the machine lacks — silently, and still reporting green.
No workflow forces them any more, so the only defence is local: `brew install
dash ksh` if you touch `instance/dex`, and set `DEX_SHIM_SHELLS="sh bash dash
ksh"` to turn a missing interpreter into a failure instead of a skip.
Bashisms in that file are invisible under macOS's bash-backed `/bin/sh` and
fatal in an instance, and nothing downstream will catch one.

**Live checks** (`.github/workflows/live.yml`, daily plus manual dispatch,
never a gate). `pytest -m live` watches third parties for drift, which happens
on their clock and has nothing to do with release timing. The workflow splits
in two: the checks a datacenter IP can be trusted with (arXiv, the iTunes
lookup, the six GitHub contents/matching-refs checks through an authenticated
`gh`, the Wikipedia page-audio extraction) fail the run and mail the
maintainer, which is
GitHub's own default for a failed scheduled workflow and needs no robot in the
issue tracker. Everything marked `ci_hostile` — fxtwitter, the two YouTube
root-namespace checks, wayback, instagram.com's bot-UA `og:` fetch and the
three embed-proxy probes, the two whisper HuggingFace fetches (the ~75MB
model, the ~2.4MB tokenizer) — runs in a `continue-on-error` job that never
notifies, because those endpoints answer a runner differently from a laptop
and a false alarm every morning trains you to ignore the real one. Read that
job when a driver misbehaves in the field.

`live.yml` pins `actions/checkout` to its major, which moves on its own.
`astral-sh/setup-uv` cannot be: astral-sh publishes bare-major tags only up to
`v7`, so `@v10` does not resolve and the pin is an exact release. It needs
bumping by hand, and nothing will tell you — check it when a runner image
deprecation bites, or once a year.

One trap: GitHub disables a scheduled workflow after 60 days without a commit
to the repo, and only emails to say so. A quiet quarter therefore stops the
drift watch silently — re-enable it from the Actions tab, or fire it once by
hand with `workflow_dispatch`.

**The release gate** (`.mint.toml`, `[release.hooks] preflight`). The four
gates run before Mint does anything else — before the AI notes, before the
review, and long before the tag. A failure aborts with no bookkeeping commit
and no tag, which is the point: instances pin tags, so a broken tag reaches
every instance at its next sync and the only rollback is hand-editing pins.
`pre_tag` bumps pyproject and uv.lock together to the release version
(`uv version "$MINT_NEW_VERSION"` — mint's hooks run on the pre-bump tree, so
left to mint the bump lands a pyproject that disagrees with the lock, which is
how the v0.1.1 tag failed every `uv sync --locked`), then builds the wheel at
that same version. The live suite stays out, and so does
mutation testing — hours, not seconds. The mutation audit belongs to the
FEATURE, not the release: releasing is the owner's act, so an audit
deferred to "before the tag" is an audit nobody runs. Finishing any work
that touched pipeline logic includes auditing the modules it changed with
`./mutate` (below) and reading the survivors — a feature is not complete,
and not reported complete, until that has happened.

**Mutation testing** (`mutmut`, in the `mutation` dependency group; `./mutate`
at the repo root is its entry point). Not an automatic gate, not in any
workflow: a whole-repo run against 1800 tests takes hours and buries the
answer. It is an
audit you point at ONE module you suspect, and the closing step of any
feature that touched pipeline logic —

```
./mutate run "dex_engine.pipeline.classify.*"
./mutate results | grep -v "not checked"
./mutate show dex_engine.pipeline.classify.x_classify_http__mutmut_2
```

A module takes a minute or two. Reading the result is the part that matters:

- `killed` (🎉) — a test failed when the line changed. That is the claim the
  test makes, verified.
- `survived` — every test still passed with the code altered. Look at the diff
  before believing it. A survivor that flips a boundary (`< 300` → `<= 300`)
  or drops a branch is a real hole: some test asserts it covers that line and
  does not. A survivor that rewrites a `reason` string or a log message is an
  **equivalent mutant** — the behaviour under contract did not change, and
  writing a test to kill it would only pin prose. Not every survivor is a bug;
  the tool finds candidates, you decide.
- `timeout` — the mutant looped forever. Counts as killed.

One collection trap, hit in the field: a module-glob run can silently omit a
function — its mutants are generated under `mutants/` but never scheduled, and
`./mutate results` shows no row for them, which reads as a clean audit. After
any run, check the results listing names every function you touched; force a
missing one by explicit name (`./mutate run
"dex_engine.normalize.x__copy_once__mutmut_*"`).

A second trap, also hit in the field: mutmut skips every DECORATED class
outright, so the methods of a `@dataclass` (`_Drain` is one) generate no
mutants at all — no rows, nothing to schedule, and an explicit run against
one aborts with "nothing matches". An audit of such a method is hand work:
apply the operator with an edit, run the suite, revert.

`mutants/` is a scratch copy of the tree; it is gitignored and safe to delete.
Two accommodations make a run possible at all, and both are commented where
they live: three tests are deselected in `[tool.mutmut]`, because mutmut
instruments by renaming functions and writing mutant copies beside them, and
those three assert on a traceback frame's function name
(`tests/pipeline/test_issues.py`) or count a call in the module's source text
(`tests/pipeline/test_ledger.py`); and `tests/conftest.py` relaxes one
hypothesis health check when `MUTANT_UNDER_TEST` is in the environment,
because mutmut calls every property twice in one process and hypothesis reads
that as two executors. Neither weakens an ordinary run.

## When you change things (anti-drift rules)

- **Designs graduate out of the backlog, one file each.** The path is:
  `backlog/<name>.md` holds an idea until it is picked up → the idea is
  discussed properly with the owner → the result of that conversation
  becomes its own `design/<name>.md`, and the backlog file and its index
  line are deleted. That is the convention; a new design belongs in a new
  file, never folded into an existing one because the existing one is
  recent. The backlog is neither a design surface nor a work log.
- **A shipped design describes its moment, not the present.**
  `design/ingestion-pipeline.md` is the largest and most recent, and it is
  still a snapshot: it is why the pipeline is shaped as it is, not proof
  that any given line still holds. Where it disagrees with the code, the
  code wins and the doc is stale. Where it disagrees with what the owner
  now wants, the owner wins. Consult designs for reasoning and prior
  argument; verify current behaviour by reading the code or running it.
- **Comments earn their place — and never reference the design doc.** A
  comment or docstring exists only for what the code cannot say: a
  non-obvious constraint, a deliberate surprise, a wild-data fact, a why.
  No narration, no restating the line below, no traceability breadcrumbs
  ("§14", "phase-3 review", "per the design"). When a constraint from the
  design matters at a call site, inline it in full, self-contained — the
  design doc never ships to instances, so a pointer to it is broken on
  arrival, and even in-repo it rots. This file and the backlog are the only
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
- **The gates move together**: the four commands in Development are named in
  two places — `.mint.toml`'s `preflight` and the Development section itself.
  Change one and change both, or the release stops agreeing with this file
  about what "green" means.
- **A new live check picks a side**: mark it `ci_hostile` (with the reason in a
  comment on the class) if a datacenter IP would be answered differently, or
  leave it unmarked and accept that a failure mails the maintainer at 06:17
  UTC. There is no third option, and an unmarked flaky check is how a daily
  alarm becomes background noise.
- **Adding a dependency**: re-lock in the same commit (`uv lock`) — CI syncs
  with `--locked` and the release gate does too, so a stale lock fails both.
- **Decisions**: record in the design file the work belongs to — its own
  `design/<name>.md` for work that graduated from the backlog. The backlog
  takes what has not been picked up yet: one file per idea, indexed in
  `backlog/index.md`. Design files are dated snapshots; this file and the
  docs are the present tense, and describe what IS, not what was.
- **README**: its under-the-hood section mirrors the structure and design
  above — keep it true whenever any of this changes.
- **`docs/shortcut.md` is device-verified**: never guess iOS Shortcuts UI —
  verify on a device or ask the owner what their screen shows.

## When you review

- **Verify by running the code, not by reading it.** Build a scratch instance
  and drive the verbs. Every finding states the trigger that reaches it and
  says whether it was reproduced or only reasoned about. Reading a line and
  imagining a path to it produces findings that cost more to triage than they
  are worth.
- **Findings are fix-or-drop.** A finding is an issue when something that
  actually happens can trigger it: a jumped clock, an interrupted run, an
  LLM-authored input file, a malformed file, a weird URL. It is not an issue
  when it needs a hand-edit of a file the contract forbids hand-editing, or a
  writer that does not exist — drop it, and don't record it. There is no
  backlog tier for review findings: a finding is fixed now or dropped, and
  `backlog/` holds only work not yet picked up — future features and
  standalone fixes, never a deferred finding.
- **Stop when a cycle returns nothing above that bar.** Early cycles on new
  work legitimately surface majors, and they get fixed. Later cycles surface a
  long tail that costs more to chase than to ship. Say the surface is done and
  stop reviewing it.
- **Decide the cheap ones.** A low-likelihood finding with a one-line fix is
  not a decision to escalate. Make the call, do it, and say what you decided.
