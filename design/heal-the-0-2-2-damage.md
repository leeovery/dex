# Heal the 0.2.2 damage through a directive

> Designed 2026-09-28 with the owner, from the backlog note of the same
> name, after a read-only survey of the owner's five instances measured
> what 0.2.2 left. Ships as directive 3, in a release of its own, after the
> forward fix that stops silent videos being transcribed.

Engine 0.2.2 shipped four healing migrations (15 to 18) that queued a live
re-read of content instances had already stored, and a transcriber for x
posts' video. Some re-reads came back worse than the copies they replaced,
a video with no speech in it came back as invented text, and a transcript
landing deleted the post's video with its description. 0.2.3 stopped the
waves and gave back the videos history held; what landed stands. This
directive asks each instance's own session to repair what remains, from
the instance's git history, with judgment.

## Why a directive

Nothing mechanical can tell a worse re-read from a better one: counts of
fence lines and table rows were tried in 0.2.3 and put back worse copies,
and most re-reads that held fewer rows had dropped separators or junk
rows. Only a reading decides. The owner's rule, written into CLAUDE.md
after 0.2.2: stored content is left alone by default, and damage a release
really did is healed from git history, by a migration only where it can
prove exactly what was damaged, otherwise by a directive. Nobody can
inspect another owner's instance, so the session that can read both copies
does the judging, and every instance heals through the same mechanism: the
owner's own instances were not repaired by hand first.

## Do no harm

The owner's words, and the directive's first rule: a slightly degraded
knowledge base is better than a broken one. If in doubt, leave it alone.
Change something only where the session can show that 0.2.2 made it worse,
and repair it from git history.

**No page is re-fetched.** The owner allowed rewrite, re-fetch or restore;
a re-read of a live page is how the damage was done, and nothing can judge
one before it lands, so the directive restores from history instead. The
one fetch is a video 0.2.2 deleted that history cannot give back: the same
bytes from the same address, asked for again with `enrich mark`.

## What the engine finds, and what the session judges

Code finds, the session judges; the session never goes hunting. Everything
is read from git at two fixed commits: HEAD, which the run's guard keeps
clean before any directive, and the **earlier commit**, the parent of the
one that first recorded migration 15, which holds the instance as it stood
before the heal ran. The materials list, per instance:

1. **Pages a re-read replaced** that lost a word, a code fence or a content
   table row against their earlier copy, with the words lost, the lines the
   copy now lacks, the lines gained, and whether the digest was written
   since. A post is compared without its transcript, which list 3 judges.
   The session keeps, restores (byte for byte) or merges each.
2. **Re-reads that lost nothing**, listed for the account and owing nothing.
3. **Transcripts on x posts** that an engine from 0.2.2 up to 0.2.6, the
   first to ask for speech (#184's fix), wrote, with the post and the
   transcript. The session keeps speech and takes out what is not, with any
   wiki statement drawn only from it.
4. **Digests written while a video was gone**: after the earlier commit and
   before migration 20 gave the video back.
5. **Files a re-read removed** from an item it touched, left gone unless
   one held knowledge the item now holds nowhere else, which then goes into
   the digest.
6. **Videos no history holds**: requeued after the directive is recorded.

Left alone, on purpose: duplicate pictures a re-read's re-signed image URLs
landed twice (clutter, not loss, and closing one needs a ledger verb);
pages whose first landing came through the heal (nothing earlier to lose);
and a session's own edits during the 0.2.2 runs, made with both copies in
view.

## Nothing deleted; digests revised by their verb

The first draft had the session delete each digest drawn from a copy it
replaced, for the run's backstop to write again. Clone sessions of the
owner's instances showed both halves wrong. An unattended session in auto
mode is refused `rm` and `git rm` of a tracked file as irreversible, so the
directive could never complete, and a directive a run cannot finish is
performed again every run while content work waits. And a digest can hold
knowledge nothing else in the item holds: one carried facts from a live
reading of a page that no enrichment file records.

So nothing is deleted. Each affected digest is settled by judgment, kept
when it states nothing that is no longer so, or revised through
`enrich item digest` with every fact still true carried forward and what
the repair brought back added. The framework gained the one thing that
needed: a directive names in `PERMITS` the `dex-enrich` verbs its own work
runs, and those run while every pending directive permits them; every other
content command still refuses. Directive 3 permits the digest verb alone.
A directive never asks a session to delete a tracked file.

On the owner's largest instance the materials list 95 pages to judge (about
207,000 characters), most of them page chrome a session keeps at a glance.

## A merge is the copy now plus a dated section

The first merge rule had the session splice each passage only the earlier
copy held back where it had stood. Clone sessions showed what that makes of
a page the site had changed in the meantime: a models table holding both
an old model's row and the row that replaced it, and a price list mixing
two months' plans, a page that never existed at any moment. A merge now
leaves the copy now as it stands and adds one section after it, before any
transcript, headed `# From the copy saved on <date>` with the earlier
copy's fetch date, holding each passage of knowledge only the earlier copy
held, word for word. What the site has dropped reads as what it was, and a
copy that was only read short loses nothing by the heading. A passage the
copy now holds in an updated form, a changed number, name or version, or
the same facts reworded, was not lost and stays out. The heading is level
one because the passages bring their own section headings, which a
second-level heading would leave reading as outside it.

The fixed shape makes the merge checkable: the copy now must be intact
above the section, its transcript below it unchanged, and every line of the
section a line of the earlier copy. A session that damaged the page while
merging, or paraphrased, is refused. The merge is one edit to the page
file, never a page assembled from numbered line ranges: a clone session
that stitched pages together from line ranges was refused by auto mode's
permission check, and a line number one off loses a line without a trace.

A github link into a repository, where the earlier copy is the repository's
README, stays corrected even when the correct reading is a thin folder
listing, and its digest owes nothing for the README: the README was never
what the link named, and 0.2.2's re-read fixed those items rather than
damaging them. One round told the session to carry each README's knowledge
into the digest where nothing else in the item held it. On the owner's
largest instance that revised 20 digests with facts about the repository
around the thing shared, such as a coding tool's install commands in the
digest of a technique write-up shared from inside its repository, which
is noise in a digest rather than knowledge restored.

## A permission refused is still filed

A clone session stopped by its own permissions held back the issue the
failure procedure asks for, reading the refusal as its environment's fault
rather than an engine defect. That leaves an instance whose directive can
never complete, every content command refused, and nobody told. A directive
must complete in an unattended run, so one that did not is the engine's to
fix whatever stopped it, a refused permission included, and the failure
procedure says so.

## Testing it

Subagent sessions pointed at clones gave the first rounds of findings, but
auto mode judges their commands in the context of the public engine
repository they were started in. The last rounds ran what a scheduled run
is: a headless Claude Code session in auto mode, started in the clone,
nobody answering a prompt, the instance's own CLAUDE.md, contract and
skills loaded, the clone's machinery brought to the engine under test by
that engine's own `sync()`, issues kept local and the remote unpushable.
Two of those sessions, running side by side, wrote the materials to the
same file under `/tmp`, and each read the other's instance; both noticed.
Scheduled runs of several instances on one machine can overlap the same
way, so the instructions keep the materials, and every file of the
session's own, under the instance's `cache/`.

## The check

The session records one outcome per listed page, transcript and settled
digest in `cache/directive-3.md`. The check holds the record to the files:
a kept page, a transcript kept as speech and a kept digest are unchanged, a
restored page is its earlier copy byte for byte, a merged page is its copy
now with the one dated section of the earlier copy's lines, a transcript
taken out is gone, a revised digest was written again, no file is deleted,
and nothing outside the listed files, their digests, the digest pass log
and `wiki/` has changed. A page is judged apart from its transcript: a post
listed both as a page and as a transcript can be kept as a page with its
transcript taken out. The check verifies that every outcome was recorded
and is what the files hold, never that the judgment was right; that rests
on the instructions and on running the directive over clones of the
owner's instances, with real sessions, before it ships.
