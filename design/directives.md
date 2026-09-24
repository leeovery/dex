# Directives

> Designed 2026-09-24, out of a working conversation with the owner while
> an instance's scope was being reframed as a lens. No backlog file
> preceded it, and it is not yet implemented. Its first two directives are
> specified in `design/instance-lens.md`, the design that needed the
> mechanism.

A directive is an instruction the engine ships to every instance for the
run session to carry out: work on an instance's own files that needs
judgment, which a migration is not allowed to do. Sync lists the directives
an instance has not completed, the run performs them before it processes
anything, and a verb records each one only after a check written in code
confirms the result.

## Why

Everything that keeps an instance current today is either synced machinery
or a migration. Sync overwrites engine-owned files wholesale. A migration is
mechanical by rule: it may rewrite state it can prove safe to rewrite, seed
the queue, or restore a file from the instance's git history, but it never
transforms content. Neither can change a file the owner wrote, because doing
that well means reading prose and deciding where each part of it belongs.

The run session can do exactly that, because every run is a Claude session
and the reading, assessing and writing that make a dex useful are already the
session's job; the engine's code exists to wire that work together and make
the mechanical parts deterministic. Two channels from the engine to the
session already exist, and neither is enough:

- A migration's `skipped` and `anomalies` hand repairs to the session, but
  only once. The migration is logged as applied whatever the session then
  does, so a repair an unattended session never makes is never offered
  again.
- The sync report's **Chat connection** line asks the session to act on a
  condition sync re-derives every time from the machine. It works because
  the condition can be read afresh on each run, so there is nothing to
  record as done.

A scheduled run usually has no reader, so an engine change that depends on
someone reading a report and acting on it will not reach most instances.
Directives are the tracked form of the first channel: the engine states the
work once, every instance performs it on its next run with nobody present,
and the instance's own state records that it happened.

## What a directive is

- **Numbered and shipped in the engine.** Directives live beside
  migrations, numbered with plain integers in a sequence of their own and
  released with the engine version that introduced them. Each one carries
  an intent line for the sync report, its instructions (a markdown body the
  session reads and performs), and a check: code that inspects the instance
  afterwards and names every condition not yet met.
- **Performed by the run session**, with the instance's skills loaded: the
  same reader every other skill text is written for.
- **Unattended, always.** A directive never asks the owner anything and
  never waits for an answer. Whatever it needs must be recoverable from the
  instance itself: its files, its state, and its git history, which it may
  read through `git log` and `git show` exactly as migrations already do.
- **Full authority over the files it names**, `state/config.json`
  included. The contract's rule that unattended sessions never edit config
  exists to stop a run from making policy on its own. A directive is the
  engine's decision, written, reviewed and rehearsed before release, so that
  rule does not apply to it.

## The flow

```
bin/dex sync ──► commit ──► guard ──► pull ──► directives ──► inbox ──► processing
     │                                             │
     └─ report lists pending directives            ├─ bin/dex directive list       pending, in order
                                                   ├─ bin/dex directive show <n>   its instructions
                                                   ├─ the session does the work
                                                   └─ bin/dex directive done <n>   check, then log
```

- Sync lists pending directives on its report with their intents, so a
  session sees them at the same moment it sees migrations.
- They are performed after the pull rather than straight after sync,
  because another machine may already have performed one and the pull
  brings its log line. `bin/dex directive list` computes the pending set
  from the log as it stands after the pull, and that is the list the run
  acts on.
- They are performed before the inbox is touched, because a directive can
  change how items are read; the lens directive does.
- Directives run in numeric order, and the first one that cannot complete
  stops the rest for this run, since a later directive may depend on an
  earlier one (the README directive links to the file the lens directive
  creates).
- Each completed directive is one commit, `directive <n>: <intent>`, holding
  its edits and its log line, pushed with the run's normal push.

## Completion goes through a verb

`bin/dex directive done <n>` runs the directive's check. If every condition
holds, it appends `{number, engine, date}` to `state/directives.jsonl`; if
any does not, it prints each unmet condition and writes nothing. The session
supplies the judgment and the engine verifies the outcome, the same split
that governs digests and placement.

The check confirms only what code can see: a file exists and is not empty,
config still parses, a link is present. It cannot judge whether a rewrite
was faithful, and the instructions and the release rehearsal carry that
weight. The check exists so that a session which ran out of time, misread
its instructions or edited the wrong file cannot record the work as done.

`state/directives.jsonl` is append-only and already covered by the
`state/*.jsonl merge=union` rule. Two machines that both perform a directive
merge to two lines for one number, which reads the same as one.

## When a directive cannot complete

Nothing is recorded, so the next run tries again, and the run's closing
report names the directive and what its check said. A directive the engine
shipped that fails its own check on an instance is an engine defect: the
session files it with `bin/dex issue`, in the abstract terms that verb
requires, and the verb's dedup keeps a directive that fails on every run
from filing more than once.

## New instances

`dex-new` records every directive the engine ships as done when it creates
an instance. A fresh instance is born in the shape the directives exist to
reach, and performing them would spend a session's effort converting
nothing.

## Authoring rules

- One directive does one job over one source, and accounts for all of it.
- Instance-blind, like everything in the engine. A directive may not name
  an instance, an owner or a path outside the instance, and it must cope
  with shapes it has never seen, because it will run on instances nobody
  maintaining the engine can read.
- Only for work that needs judgment: anything code can do safely stays a
  migration, and a directive is never the easy way out of writing one.
- Always completable without the owner, and always carrying a check.
- Content is moved, never silently lost: when a directive removes text that
  has no new home, its commit message names what it removed, and git
  history keeps it.
- Rehearsed before release by driving a real run over copies of every
  instance the maintainer can reach, including heavily customised ones.

## What changes in the engine

- A `directives/` package beside `migrations/`: discovery, the log, and one
  module per directive exposing its number, intent, instructions and check.
- A `dex-directive` command with `list`, `show <n>` and `done <n>`, landing
  in the four places a new command touches: the `pyproject.toml` entry
  point, the `instance/dex` usage line, the README command table and
  `KNOWN_VERBS` in `pipeline/observed.py`.
- `run_sync` computes the pending set after migrations, and the sync-report
  surface gains a directives section.
- `dex-new` seeds `state/directives.jsonl` with every shipped number.
- The dex-run preparation reference gains the directives step between the
  pull and the inbox, and the run's closing report covers directives
  performed, pending and failed.
- The contract states a directive's authority beside the config rule it
  sets aside.
- `state-formats.md` documents `state/directives.jsonl`.

## Deferred, deliberately

- Whether a migration that can see its own leftovers should ship a
  directive for them instead of using `skipped` and `anomalies`. The
  one-shot channel stays as it is until a migration needs more.
- Directives aimed at some instances and not others. Every directive runs
  everywhere, and an instance with nothing to do for one completes it by
  finding nothing to do.

## Settled during implementation

Recorded 2026-09-24, while the stack that builds this design was reviewed.

- **Materials.** A directive may define `materials(root)`: text the engine
  generates for the instance, which `bin/dex directive show <n>` prints after
  the instructions between two marker lines, and `show <n> --materials`
  prints alone, byte for byte, so a session can write a file the check
  compares exactly. It keeps engine templates in one place instead of copied
  into instructions.
- **`done` refuses in a fixed order:** an unknown number, then a directive
  already recorded (exit 0, nothing written), then one whose predecessor is
  still pending, then an unmet check, and only a passing check appends.
- **A refused `done` leaves nothing behind.** The run restores the tree to
  HEAD (`git reset --hard HEAD`, then `git clean -fd`, safe because the guard
  left the tree clean), performs no later directive, files the issue and
  continues with the inbox.
- **The dedup holds because the wording is fixed.** The issue fingerprint
  hashes the verb and both clauses as written, so `preparation.md` prescribes
  the exact `expected` and `observed` clauses for a directive that cannot
  complete, with the detail in `steps` and the local `note`.
- **A run that died mid-directive is never recovered.** The guard restores a
  tree holding only a pending directive's edits to HEAD, and the directive
  runs again from the start: half a judgment nobody checked is not work.
- **Standalone health checks perform pending directives too,** because
  dex-lint's preparation is the run's.
- **`dex-new` writes nothing when no directive ships;** a missing log reads
  as empty. The log's reader and appender are shared with migrations
  (`numbered_log.py`).
- **Instructions run past shell aliases.** An owner's shell can alias
  `diff`, `ls` or `grep` to something else, and a scheduled session runs in
  that shell, so every such command an instruction gives is written as
  `command diff` and the like, and a template test fails on a bare one.
