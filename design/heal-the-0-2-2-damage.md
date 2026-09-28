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
   table row against their earlier copy, with the words lost, the lines
   holding them, the lines gained, and whether the digest was written since.
   The session keeps, restores (byte for byte) or merges each.
2. **Re-reads that lost nothing**, listed for the account and owing nothing.
3. **Transcripts on x posts** that an engine from 0.2.2 up to the one that
   first asks for speech wrote (#184's fix), with the post and the
   transcript. The session keeps speech and takes out what is not, with any
   wiki statement drawn only from it.
4. **Digests written while a video was gone**: after the earlier commit and
   before migration 20 gave the video back.
5. **Videos no history holds**: requeued after the directive is recorded.

Left alone, on purpose: duplicate pictures a re-read's re-signed image URLs
landed twice (clutter, not loss, and closing one needs a ledger verb the
directive cannot run); readings migration 15 deleted whose re-read died
(each was the repository's README stored for a link to something else);
pages whose first landing came through the heal (nothing earlier to lose);
and a session's own edits during the 0.2.2 runs, made with both copies in
view.

## Git and file edits only

Every `dex-enrich` command refuses while a directive is pending, so the
session cannot write a digest or a ledger line during it. A digest drawn
from a copy the session replaced is deleted instead: once the directive is
recorded, the run's backstop lists every item without a digest under
**Digest these**, and the run writes it from the item as it stands. That
keeps the directive's own work to reading, restoring and recording, which
matters because a directive a run cannot finish is thrown away and tried
again every run while content work waits. On the owner's largest instance
the materials list 66 pages to judge (about 130,000 characters).

## The check

The session records one outcome per listed page and transcript in
`cache/directive-3.md`. The check holds the record to the files: a kept
page and a transcript kept as speech are unchanged, a restored page is its
earlier copy byte for byte, a merged page differs from both copies, a
transcript taken out is gone, exactly the digests the rules name are
deleted, and nothing outside the listed files, their digests and `wiki/`
has changed. It verifies that every outcome was recorded and is what the
files hold, never that the judgment was right; that rests on the
instructions and on running the directive over clones of the owner's
instances, with real sessions, before it ships.
