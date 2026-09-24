# The instance lens

> Designed 2026-09-24, out of a working conversation with the owner. No
> backlog file preceded it, though it may absorb
> `backlog/per-instance-context.md` (see the open questions). It is not yet
> implemented, and it depends on `design/directives.md`, and its two directives
> are specified here.

An instance's scope stops being a check at the door and becomes the lens its
content is read through. When you share something into an instance you have
already decided it belongs there, so the instance's job is to read it from
its own angle: the lens decides what gets fetched, which facts are
extracted, how topics break apart and what the wiki writes about. The same
link shared into three instances becomes three different items, each read
for what that instance cares about.

## Why

**Sharing is the curation.** A gardening site shared into a design instance
is there because it is beautiful and worth taking apart as design. The same
site shared into a business instance is there because the business is well
run, into a marketing instance because its landing page nails social proof,
and into a gardening instance for the gardening. Today every one of those
instances except the gardening one would reject the share at the door,
because its subject is gardening.

**The door check has almost nothing to go on.** The scope check in
`ingest-item.md` runs before the item is created and before anything is
fetched, so it judges the capture file alone: a URL and your note, plus any
attached media. For a bare link to a post that is a status id. A session
either guesses from the URL or looks at the content outside the pipeline,
and an unattended run is told to skip anything borderline and leave it in
the inbox for a decision nobody is there to make.

**The lens has nowhere to act.** Even when a share gets past the door, the
digest step extracts "one standalone fact per fact the source actually
yields" with no reference to the instance, and the harvest step's examples
of primary artifacts (the repo, the docs, the paper) assume an engineering
reader. Two instances given the same post write the same digest.

## The ruling

- Everything you share into an instance is read through its lens.
- The only drop is when reading the item through the lens yields nothing:
  a share into the wrong instance, or content with nothing in it. A dropped
  item is named in the run report with its reason.
- Content the engine cannot reach is not content with nothing in it. A
  paywall, a blocked fetch or a dead link is a fetch problem, handled by the
  parking lanes and the heal procedure exactly as today.
- Overlap between instances is expected and never resolved, and no
  instance's text may route content to a sibling.

## The pipeline, reordered

The lens verdict needs the content, so it moves to after enrichment and
harvest:

```
before   capture ─► scope check ─► item new ─► enrich ─► harvest ─► digest ─► place ─► wiki
                    URL + note only

after    capture ─► item new ─► enrich ─► harvest ─► lens verdict ─┬─► digest ─► place ─► wiki
                                                    on the content │
                                                                   └─► drop: bin/dex exclude,
                                                                       named in the report
```

- Every capture becomes an item, so `enrich item new` is no longer gated.
- The verdict follows harvest because the substance often sits one link
  away, like a post whose whole point is the article it links to.
- The verdict is one question asked of what landed: through this lens, is
  there anything here? If there is, the item goes on to its digest. If
  there is not, it is dropped through `bin/dex exclude` with a reason in
  lens terms, and the report names it.
- A parked item (waiting on transcription, blocked, manual) gets no verdict
  until its sources land, which is how parked items already wait for their
  digest. An item whose every unit is dead is judged on your note, as its
  digest already is.
- Unattended runs stop skipping borderline items. Nothing is borderline at
  the door any more, and the verdict after harvest is a judgment the run is
  equipped to make alone.

## Where the lens acts

- **Harvest** promotes the primary artifacts of the item's subject that the
  lens needs: the pricing and about pages for a business lens, the repo and
  docs for an engineering lens, the rendered pages themselves for a design
  lens.
- **Digest** extracts the facts the source yields through the lens and
  judges `signal` relative to it, so a design mockup with no product behind
  it is thin evidence to a business lens and rich material to a design one.
- **Place** breaks topics along the lens, so a design instance grows topics
  like typography and landing-page layout from the same shares a marketing
  instance files under social proof and sales copy.
- **Wiki** pages are written from the lens's angle: what the design does,
  how the business makes money, what the copy achieves.

## Where the lens lives: `lens.md`

`lens.md` at the instance root is your statement of what this instance reads
for, and it is the one file in an instance that is neither knowledge nor
machinery. You change it by asking a session; sync never overwrites it, and
an unattended run never edits it except through a directive.

```markdown
# <instance name>: <what it reads for, in a phrase>

## Reads for
- the angles this instance takes on whatever is shared into it

## Emphasise        (optional)
## Set aside        (optional)
```

`Reads for` is required, while `Emphasise` and `Set aside` are optional
guidance on how to read: a design lens might emphasise layout, typography, colour and
interaction and set aside the subject matter. Lint checks that the file
exists with a non-empty `Reads for`, and fails when it does not, because
every judgment in a run depends on the lens; the sync report carries the
same finding so a missing lens is visible before any work starts.

## CLAUDE.md becomes engine-owned

Every instance gets the same CLAUDE.md, synced from the template and
overwritten like the contract. It says the directory is a dex instance,
imports `.claude/dex-contract.md` and imports `lens.md`. Nothing
owner-specific lives in it, and no engine text may tell a session to write
into it, since the next sync would overwrite whatever was written.

The contract gains the ruling above, so every instance receives the lens
semantics through sync with no owner action.

## The README stays yours

The README is how an instance shows on GitHub, so it stays owner-owned. The
template changes: the scope section goes, the instance name and the repo in
the join prompt are filled in, and one line links to `lens.md` as this
instance's lens. Directive 2 rewrites every existing README to that template
once. After that you may change it freely, and a later directive can still
reach it if the engine ever needs to.

## Discord sources become config

The pull procedure in `backfills.md` is already engine documentation:
DiscordChatExporter through Docker, the token in `.env`, id discovery, the
export command. What it lacks is a home for the per-instance facts. It tells
sessions to keep the server and channel ids in the instance's CLAUDE.md and
to record newly discovered ids there, which under an engine-owned CLAUDE.md
destroys them at the next sync.

- `state/config.json` gains a `discord` key holding the server id and the
  channels to pull, by name and id:

  ```json
  "discord": {"guild": "<guild id>", "channels": {"<channel name>": "<channel id>"}}
  ```

  The token stays in `.env`. The shape follows what `backlog/watchers.md`
  expects a watcher's attachment to look like, so a future Discord watcher
  reads the same key.
- `backfills.md` reads the key, and when you ask for a pull while it is
  absent, the session interviews you (whether a token is present and valid,
  which server, which channels), using the discovery commands the reference
  already carries, and writes your answers to config.

## How existing instances catch up

The release that ships this makes CLAUDE.md engine-owned, so the first sync
after it overwrites every instance's CLAUDE.md. Nothing is lost, because
every instance is a git repository and the directives read your last version
from history.

**Directive 1: rehome the owner's CLAUDE.md.** It reads CLAUDE.md as it
stood in the commit before sync replaced it, and gives every section a home:

- the scope section, whatever its heading, becomes `lens.md`, its bullets
  carried over word for word under `Reads for`, with any prose about how to
  read kept under `Emphasise` or `Set aside` where it fits;
- per-instance Discord facts become the `discord` config key;
- anything with no home is removed, and the commit message names each
  removed section so history keeps it.

Its check: `lens.md` exists with a non-empty `Reads for`, and
`state/config.json` parses.

**Directive 2: rewrite the README from the template.** It fills the
instance name from the directory and the repo from the `origin` remote,
drops the scope section and links to `lens.md`. Anything in the old README
beyond the template is removed and named in the commit message, by the same
rule as directive 1. Its check: the README links to `lens.md` and carries no
scope section.

## What changes in the engine

- `instance/CLAUDE.md` becomes a synced file that `sync()` writes beside the
  contract, and `sync.py`'s docstring stops listing it as instance-owned.
- `instance/lens.md` is a new seed, and `instance/README.md` becomes the new
  template.
- `new.py` seeds `lens.md` and the README, and its closing hint says to
  fill in the lens.
- `docs/start.md` asks what the dex should read for in place of what it
  covers, writes the answer into `lens.md`, and drops the step that
  personalises CLAUDE.md.
- `instance/dex-contract.md` gains the lens semantics.
- `ingest-item.md` loses its door check (§1), gains the lens verdict after
  harvest, and reads the lens in harvest, digest, place and wiki (§5 to §8).
- `processing.md` creates an item for every capture, and `dex-run/SKILL.md`
  loses the rule that unattended runs skip borderline scope calls, while
  its closing report names dropped items.
- `dex-lint`'s judgment sweep replaces "scope creep" with pages drifting off
  the lens, and lint checks `lens.md`.
- `exclude.py`'s default reason and the exclude section of `state-formats.md`
  speak in lens terms.
- `serve/steering.py` describes each instance from `lens.md` in place of
  CLAUDE.md, and its capture routing and the `capture` tool's docstring in
  `serve/server.py` stop sending captures to "the instance whose scope this
  belongs to".
- The config parser in `pipeline/types.py` and the config table in
  `state-formats.md` gain the `discord` key, and `backfills.md` reads it.
- Directives 1 and 2, per `design/directives.md`.

## Testing

The acceptance test is the gardening share. An instance whose lens has
nothing to do with gardening, given a gardening site shared for its design,
its business or its landing page, must digest it through the lens and never
drop it at the door; a share with genuinely nothing for the lens must be
dropped and named. Before the release is tagged, both directives run in a
real session over copies of every instance the maintainer can reach,
including ones whose CLAUDE.md was customised well beyond the template, and
each copy is checked for its scope arriving in `lens.md` word for word and
for nothing leaving without being named.

## Open questions

- **Open question: backfills.** A bulk export (a chat dump, a bookmark
  file) was never curated item by item, so the signal that a share is
  relevant does not exist there. Recommendation: the backfill scope-filter
  pass stays a filter, applied through the lens, and only direct shares get
  full curation trust.
- **Open question: chat capture routing.** The chat server tells an agent to
  save into "the instance whose scope it belongs to", which is the exclusive
  framing again. Recommendation: your choice decides; when you name no
  instance and more than one lens fits, the agent asks.
- **Open question: existing digests.** Digests written before this release
  were read without a lens. Recommendation: going forward only, because
  re-reading a corpus through its lens would be a directive with a large cost, and most
  existing items were shared into the instance whose lens already matches
  their content.
- **Open question: per-instance context.** `backlog/per-instance-context.md`
  (standing context that steers scanning, enrichment and digestion) is what
  the optional sections of `lens.md` hold. Recommendation: this design
  absorbs it, and the backlog file and its index line are deleted.
