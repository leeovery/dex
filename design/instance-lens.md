# The instance lens

> Designed 2026-09-24, out of a working conversation with the owner. No
> backlog file preceded it, but it absorbs the idea
> `backlog/per-instance-context.md` held (standing guidance on how to read an
> instance's content), whose file is deleted with this design. It is not yet
> implemented, it depends on `design/directives.md`, and its two directives
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
- Items that arrive by a pull meet the same verdict. A Discord pull lands
  its export in `raw/`, and `bin/dex normalize` turns each conversation into
  an item without passing through the inbox; today a separate scope-filter
  pass then removes items before anything is fetched. That pass goes, and
  every item, shared or pulled, gets the one verdict after enrichment and harvest,
  and no stricter bar is needed for pulled items, because chatter such as a
  one-line complaint about a tool yields nothing through any lens and is
  dropped by the same question. A dropped conversation stays dropped when
  the next pull re-exports the channel, since `bin/dex exclude` records it in
  `state/exclusions.tsv` and normalize skips it.
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

## Saving from chat

The chat server lets you say "save this to my dex" from any connected
client, and the agent has to name an instance for the capture. Today it is
told to pick "the instance whose scope it belongs to", which is the
exclusive framing this design removes. The rule becomes:

- when you name an instance, the capture goes there;
- when the server serves a single instance, it goes there without a
  question;
- otherwise the agent asks which instance, or which ones, before saving.

The agent never infers the destination from the content, because any link
can belong in any instance depending on the lens you want it read through.
A gardening site might be for a gardening instance, or for design, business,
marketing or engineering, and only you know which.

## Where the lens lives: `lens.md`

`lens.md` at the instance root is your statement of what this instance reads
for, and it is the one file in an instance that is neither knowledge nor
machinery. You change it by asking a session; sync never overwrites it, and
an unattended run never edits it except through a directive.

The lens is free-form, because Claude is its only reader and reads
sentences, lists and examples equally well; an example is often the clearest way to
state taste ("a site like this, for its type pairing, not its products").
Nothing in the engine parses it, and it is read as one coherent statement,
never section by section. The seed offers headings as prompts, which you may
write under in any form, rename or delete:

```markdown
# <instance name>

## Reads for
<what this instance takes from whatever is shared into it>

## Emphasise
<what to look at hardest>

## Set aside
<what to ignore, even when it is the subject>
```

A design lens, for example, might read for how things are made to look and
work, emphasise layout, typography, colour and interaction, and set aside
the subject matter entirely. Any standing guidance on how to read this
instance's content belongs here too: what a community's shorthand means
("CC" is Claude Code in these channels), which kinds of link are noise here,
or how deep a domain deserves to go.

The contract says all of this to every session: the lens is free-form, owned
by you, and read whole. Lint fails when `lens.md` is missing or empty, and
when it still holds the seed's placeholder text, because every judgment in a
run depends on the lens and a blank one fails silently. The sync report
carries the same finding, so a missing or unfilled lens is visible before
any work starts.

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

- the scope section, whatever its heading, becomes `lens.md`, carried over
  word for word and laid out under the seed's suggested headings, with any
  prose about how to read kept under `Emphasise` or `Set aside` where it
  fits;
- per-instance Discord facts become the `discord` config key;
- anything with no home is removed, and the commit message names each
  removed section so history keeps it.

Its check: `lens.md` exists and is not empty, and `state/config.json`
parses.

**Directive 2: rewrite the README from the template.** It fills the
instance name from the directory and the repo from the `origin` remote,
drops the scope section and links to `lens.md`. Anything in the old README
beyond the template is removed and named in the commit message, by the same
rule as directive 1. Its check: the README links to `lens.md` and carries no
scope section.

## Existing digests

Digests written before this release stay as they are. Most existing items
were shared into the instance whose subject already matched them, and where
the two differ the instance's scope list was usually enough to steer the old
run toward the right angle anyway, so re-reading whole corpora through their
lenses would cost thousands of item reads for little change. Nothing extra is
needed for the corpus to move toward lens-shaped digests over time: every
digest the engine rewrites after the release, for a re-fetch or a requeued
item, is written by the new skills and so through the lens.

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
- `instance/dex-contract.md` gains the lens semantics, including that the
  lens is free-form, owner-owned and read as a whole.
- `ingest-item.md` loses its door check (§1), gains the lens verdict after
  harvest, and reads the lens in harvest, digest, place and wiki (§5 to §8).
- `processing.md` creates an item for every capture, and `dex-run/SKILL.md`
  loses the rule that unattended runs skip borderline scope calls, while
  its closing report names dropped items.
- `dex-lint`'s judgment sweep replaces "scope creep" with pages drifting off
  the lens, and lint checks that `lens.md` exists, is not empty and no
  longer holds the seed's placeholder text.
- `exclude.py`'s default reason and the exclude section of `state-formats.md`
  speak in lens terms, and `backfills.md`, `exclude.py` and `normalize.py`
  stop describing a scope-filter pass after normalize.
- `serve/steering.py` describes each instance from `lens.md` in place of
  CLAUDE.md, and its capture routing and the `capture` tool's docstring in
  `serve/server.py` state the chat rule above in place of "the instance
  whose scope this belongs to".
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

## Settled during implementation

Recorded 2026-09-24, while the stack that builds this design was reviewed,
rehearsed on copies of every instance the maintainer can reach, and run
through an acceptance test. Where a line here contradicts the sections
above, this section is the later decision.

- **No lens is a general knowledge dex (owner's ruling).** A missing or
  empty `lens.md` is a legitimate way to run an instance, never a fault:
  the instance reads for anything, every share is read for its general
  substance, and the verdict drops only content with nothing in it at all.
  A lens still holding the seed's placeholder lines, or one that cannot be
  read, reads the same way. Lint and the sync report carry a note for those
  two cases and never fail on the lens, which supersedes "Lint fails" in
  the section on `lens.md`. The chat server describes such an instance as
  a general knowledge dex, and its README says so in place of the lens
  link.
- **A look the capture could not carry is never a drop.** When the lens
  reads for an aspect the capture could not hold, such as a page's look
  when only its text landed, that is a capture gap like a paywall, and the
  item is digested from what landed and the owner's note. A rendered page
  capture for visual lenses is the fast follow
  (`backlog/page-screenshots.md`).
- **The verdict's edges.** An item that already holds a digest is past its
  verdict, so a re-fetch or requeue goes straight to its digest. A thin
  yield is a `signal: low` digest, never a drop. An item whose landed
  content only points at a unit that never landed is never dropped. The
  verdict is made alone in attended sessions too, and a drop reason never
  names another instance, so a reason cannot act as routing.
- **The harvest rules version was not bumped.** The harvest rule became
  lens relative, but a bump re-harvests every corpus, which the ruling on
  existing digests rules out.
- **`exclude` purges everything a dropped item leaves.** Beyond the corpus
  file, enrichment, digest and ledger entries, it removes the item's media
  (a file another live item lists is kept) and its `state/passes.jsonl`
  records, since the verdict now drops items after harvest. A batch is
  refused whole when an unreadable corpus file leaves its media unsettled,
  because nothing else records which media a dropped item carried.
- **The takeover waits for history.** Sync overwrites a CLAUDE.md that
  differs from the engine's only once git history holds it: an uncommitted,
  untracked or ignored copy, or one outside git, is left untouched and
  named on the sync report, and the next sync after a commit takes it over.
- **Discord channel names are directory names.** Normalize derives item ids
  from `raw/discord/<name>/`, so a channel's name in config is its export
  directory and never changes after its first pull. Setup offers existing
  exports as the answers.
- **Directive 1 never waits.** The engine finds the owner's CLAUDE.md (the
  newest committed version that is not an engine copy) and prints it as the
  directive's materials. A sentence that states what the instance reads for
  and then the old fence ("anything X-related is fair game; anything
  unrelated is out of scope") keeps its first half in the lens and loses
  only the fence. When the owner never stated a scope (no such version, or
  one still holding the old template's placeholders), the directive writes
  no lens, so the instance reads as a general knowledge dex.
- **Directive 2's check is exact.** Its materials are the README rendered
  for the instance, and `done` passes only when `README.md` equals them.
  The render links `lens.md` only when the instance has one, drops the "Run
  it on another machine" section when there is no GitHub origin (read as
  stored, past any `insteadOf` rewrite), and keeps the template's closing
  line above that section so it survives the drop.
- **The lens never narrows harvest (owner's ruling, 2026-09-25).** This
  supersedes the Harvest bullet under "Where the lens acts". A link is
  chosen before its page is read, so a lens-narrowed harvest throws away
  what nobody has seen; harvest follows the item's subject exactly as it
  did before the lens, and the lens may only add a page its angle needs.
  The lens acts where the item is read: the digest's takeaway (a design
  dex's digest of a flower site holds its design, not plant facts, as a
  designer reading it would), the topics and the wiki. The fetched content
  itself is always kept whole in the enrichment.
