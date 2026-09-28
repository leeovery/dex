# Repair what engine 0.2.2 did to stored content

Engine 0.2.2 shipped migrations that read again, from the live web,
content this instance had already stored: web pages and papers, github
links an older engine had read as their repository's README, x posts whose
video was never transcribed, and x articles stored without their links and
code. Each re-read that landed replaced the copy stored before it. A page
read again months after it was saved is often not the page that was saved:
the site has changed it, moved it, put it behind a login, or dropped what
made it worth saving, and 0.2.2's extractor lost some content older engines
kept. Many re-reads came back better. Some came back worse.

The same engine transcribed the video of every x post it read, and a video
with no speech in it came back as text nobody said: a line of emoji, the
post's own words echoed back, or sentences that make no sense. When the
transcript landed, it also deleted the post's downloaded video and the
video's description. Engine 0.2.3 put those back from git history, but a
digest written while they were gone describes the item without them.

This directive repairs what the engine can show 0.2.2 touched here. The
engine finds each case in this instance's ledger and git history and lists
it in the materials. You judge each one and repair it from git history.

## Do no harm

This rule comes before every step below. A slightly degraded knowledge
base is better than a broken one. Change a file only when comparing it
with its earlier copy shows that 0.2.2 made it worse: it lost knowledge the
earlier copy held, or it holds text its source never said. When you are in
doubt, leave it as it is. Never remove knowledge the item holds nowhere
else.

Nothing is read again from the web to repair a page: a re-read is how this
damage was done, and nothing can judge a re-read before it lands. Git
history holds every earlier copy.

This directive edits only the files the materials name, the digests of
their items, and pages under `wiki/`. It works with git and file edits
alone: every `bin/dex enrich` command refuses while a directive is pending,
and none is needed until step 6.

## The materials

The materials are printed after these instructions, between the
`===== materials` line and the `===== end of materials =====` line. They
open with the **earlier commit**: this instance as it stood just before
0.2.2's migrations ran. Every "earlier copy" below is a file as that commit
holds it. Five lists follow, each under a heading of its own, with a line
saying there is nothing in place of a list that is empty:

1. **Pages a re-read replaced**: every stored page whose re-read lost
   something its earlier copy held, whatever its kind: web page, paper,
   github reading, x post or x article. Each entry gives the file's path
   now and in the earlier commit, how many words, code fences and table
   rows each copy holds, the words the copy now lacks, the lines of the
   earlier copy holding them, the lines of the copy now holding words the
   earlier copy lacked, and whether the item's digest was written after the
   earlier commit.
2. **Re-reads that lost nothing**: pages a re-read changed without losing
   a word, a code block or a table row. They owe no judgment; they are
   listed so that every change is accounted for. Leave them as they are.
3. **Transcripts on x posts**: every x post whose stored copy holds a
   transcript that engine 0.2.2 or later wrote, with the start of the post,
   the transcript, and whether the item's digest was written after the
   transcript landed.
4. **Digests written while a video was gone**: every item whose digest was
   written after 0.2.2 deleted its video and before 0.2.3 gave it back.
5. **Videos no history holds**: every video 0.2.2 deleted that git history
   cannot give back.

When the materials say history cannot answer here, because this is no git
repository, the clone is shallow, or 0.2.2's migrations never ran, nothing
here can be shown to be worse: go to step 5.

## 1. Judge each page a re-read replaced

For each entry under **Pages a re-read replaced**, read what the re-read
lost and what it gained. The materials show both. When you need a whole
copy, `git show <earlier commit>:<earlier path>` prints the earlier one, and
the file itself is the other. Then decide one of three:

- **Kept**, when what was lost is not knowledge: navigation, banners,
  cookie and subscription prompts, share and comment widgets, lists of
  related articles, a date, a counter, a table's separator line or empty
  row, or the same words in another layout. Most entries end here.
- **Restored**, when the re-read lost knowledge its earlier copy held and
  gained none of its own: prose, code, a table's rows, a list, a caption, a
  notebook's cells. A page that now holds something else entirely, such as
  the site's current front page in place of the page saved, a login or
  error page, or another article at the same address, lost everything the
  earlier copy held. Write the earlier copy back byte for byte with
  `git show <earlier commit>:<earlier path> > <path now>`, and change
  nothing in it.
- **Merged**, when each copy holds knowledge the other lacks. Start from
  the copy that holds more, keep its frontmatter, and add each passage only
  the other copy holds, word for word, in the place it held there.

A github entry whose earlier copy is a repository's README, for a link
that names something inside the repository, was a misread the re-read
corrected: keep it. For an x post, judge the post here and its transcript
in step 2.

## 2. Judge each transcript

A transcript is **speech** when it reads as words someone said: sentences
that follow from one another and fit the post, in any language. It is
**not speech** when it is emoji or symbols, one phrase over and over, the
post's own text echoed back, subtitle credits or sign-offs for a video with
nothing said in it, or sentences that follow from nothing the post shows.
That is how a transcriber hears a silent screen recording or a music-only
clip. When you cannot tell, it is speech: keep it.

Take out a transcript that is not speech. Delete its `## Transcript`
heading and everything after it, with the blank line before the heading.
In the frontmatter, delete the `model:` line and put back the `via:` line
the file had before the transcript landed, which `git log -p -- <path>`
shows, or delete the `via:` line when it had none. Keep every other line,
`enclosure:` included. Then search `wiki/` for the item's id, and take out
any statement a page makes that came only from that transcript.

## 3. Remove each digest drawn from what you replaced

A digest written from a copy that this directive replaces states what that
copy said. Delete `state/digests/<id>.md` for:

- each item where you restored or merged a page, or took out a transcript,
  when the materials say its digest was written after the earlier commit,
  or after the transcript landed; and
- each item under **Digests written while a video was gone**.

Deleting a digest is how this directive has it written again. Once the
directive is done, the run's backstop, `bin/dex enrich status`, lists under
**Digest these** every item that has no digest, and the run writes each one
from the item as it now stands. Git history keeps the digest you delete. A
digest written before the earlier commit was drawn from the earlier copies
and still holds, so keep it, and delete no digest this step does not name.

## 4. Record each outcome

Write `cache/directive-3.md` with one line for every entry under **Pages a
re-read replaced** and every entry under **Transcripts on x posts**, each
naming its path exactly as the materials give it. An x post listed under
both takes a line for each.

```
- <path>: kept — <why, in a few words>
- <path>: restored — <what the re-read lost>
- <path>: merged — <what each copy alone held>
- <path>: speech
- <path>: not speech — <what it was>
```

## 5. Check and record

Run `bin/dex directive done 3`. It confirms that the record has a line for
every entry, that each kept page and each post whose transcript is speech
is unchanged, that each restored page is its earlier copy byte for byte,
that each merged page differs from both copies, that each post whose
transcript is not speech holds none, that exactly the digests step 3 names
are gone, and that nothing outside the files this directive names has
changed. It records the directive only when every condition holds. Run it
even when you could not finish the steps above, and when it refuses, do
what its output says.

## 6. Ask again for each video no history holds

Once the directive is recorded, `bin/dex enrich` runs again. For each video
under **Videos no history holds**, run `bin/dex enrich mark <url> queued`,
with the video's address exactly as the materials give it. The run then
downloads it again where the address still serves it, and it owes a
description like any other download. This is the one thing this directive
fetches: the video itself, which nothing else can give back.

## 7. Commit

Commit every file you changed or deleted, the ledger when step 6 changed
it, and `state/directives.jsonl` together as one commit. The subject is the
first line `bin/dex directive show 3` printed, `directive 3: ` followed by
this directive's intent. The body names every change, so the owner can
find each earlier copy:

```
Earlier copies: git show <earlier commit>:<earlier path>

Restored:
- <path>: <what the re-read lost>

Merged:
- <path>: <what each copy alone held>

Transcripts taken out:
- <path>: <what it was>

Digests to write again:
- <item id>

Videos asked for again:
- <url>
```

Leave out a list that would be empty. When nothing changed, the body is one
line saying so. Write the message to `cache/directive-3-message.txt` and
commit with `git commit -F cache/directive-3-message.txt`, which keeps its
quotes and backticks exactly as written.
