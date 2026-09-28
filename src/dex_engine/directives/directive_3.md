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
else. What counts as knowledge is what this instance reads for, as its
`lens.md` states, or everything of substance when it has no lens; a page's
navigation and chrome never are.

Nothing is read again from the web to repair a page: a re-read is how this
damage was done, and nothing can judge a re-read before it lands. Git
history holds every earlier copy. Nothing is deleted either: every file
this directive changes is changed in place.

This directive edits only the files the materials name, the digests of
their items, and pages under `wiki/`. It works with git, file edits and one
engine verb, `bin/dex enrich item digest`, which runs while this directive
is pending. Every other `bin/dex enrich` command refuses until it is done.

## The materials

The materials are printed after these instructions, between the
`===== materials` line and the `===== end of materials =====` line. They
open with the **earlier commit**: this instance as it stood just before
0.2.2's migrations ran. Every "earlier copy" below is a file as that commit
holds it. Six lists follow, each under a heading of its own, with a line
saying there is nothing in place of a list that is empty:

1. **Pages a re-read replaced**: every stored page whose re-read lost
   something its earlier copy held, whatever its kind: web page, paper,
   github reading, x post or x article. Each entry gives the file's path
   now and in the earlier commit, how many words, code fences and table
   rows each copy holds, the words the copy now lacks, the lines of the
   earlier copy the copy now lacks, the lines of the copy now the earlier
   copy lacked, and whether the item's digest was written after the
   earlier commit. An x post is compared without its transcript, which
   list 3 judges.
2. **Re-reads that lost nothing**: pages a re-read changed without losing
   a word, a code fence or a table row. They owe no judgment; leave them as
   they are.
3. **Transcripts on x posts**: every x post whose stored copy holds a
   transcript an engine wrote without first asking whether its video held
   speech, with the start of the post, the transcript, and whether the
   item's digest was written after the transcript landed.
4. **Digests written while a video was gone**: every item whose digest was
   written after 0.2.2 deleted its video and before 0.2.3 gave it back.
5. **Files a re-read removed**: every file an item held at the earlier
   commit that it holds nowhere now, where a re-read touched the item.
6. **Videos no history holds**: every video 0.2.2 deleted that git history
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
  the copy that holds more, keep its frontmatter, and add each passage of
  knowledge only the other copy holds, word for word, in the place it held
  there.

A github entry whose earlier copy is a repository's README, for a link
that names something inside the repository, was a misread the re-read
corrected: keep it.

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
In the frontmatter, delete the `model:` line, and change the `via:` line,
where it stands, back to the value it had before the transcript landed,
which `git log -p -- <path>` shows; delete the `via:` line when the file had
none. Keep every other line, `enclosure:` included. Then search `wiki/` for
the item's id, and take out any statement a page makes that came only from
that transcript. `wiki/log.md` is the record of past runs: leave it.

## 3. Settle each digest

A digest written from a copy you replaced states what that copy said, and
one written while a video was gone describes the item without it. Settle
the digest, `state/digests/<id>.md`, of:

- each item where you restored or merged a page, or took out a transcript;
- each item under **Digests written while a video was gone**; and
- each item under **Files a re-read removed** whose removed file held
  knowledge the item now holds nowhere else.

Read the digest beside the item as it now stands, then do one of two:

- **Keep** it when it states nothing that is no longer so and misses
  nothing of substance the item now holds.
- **Revise** it otherwise. Write its payload with every fact the digest
  states that is still true, corrected where the repair changed it, and
  what the item now holds that the digest lacks: the knowledge of a page
  you restored, a video that came back, a removed file's knowledge that
  lives nowhere else. Keep its signal, topics and entities unless the
  repair makes one wrong. Write the payload to `cache/digest.json` in the
  shape the run's ingest procedure uses (`{"id", "signal", "topics",
  "facts"}`, `entities` optional), and run
  `bin/dex enrich item digest --file cache/digest.json`. The verb writes
  the file, derives its date and its media list from what the item holds
  on disk, and records the digest pass. A fact left out of the payload is
  gone from the digest, so carry every one that still holds.

Never delete a digest, and never edit one by hand: the verb is its writer.
Leave every other file under **Files a re-read removed** gone; git history
keeps each of them.

## 4. Record each outcome

Write `cache/directive-3.md` with one line for every entry under **Pages a
re-read replaced**, every entry under **Transcripts on x posts**, and every
digest you settled in step 3. Name each path exactly as the materials give
it, without backticks. An x post listed under both lists takes a line for
each.

```
- <path>: kept — <why, in a few words>
- <path>: restored — <what the re-read lost>
- <path>: merged — <what each copy alone held>
- <path>: speech
- <path>: not speech — <what it was>
- state/digests/<id>.md: kept — <why, in a few words>
- state/digests/<id>.md: revised — <what changed>
```

## 5. Check and record

Run `bin/dex directive done 3`. It confirms that the record has a line for
every entry and every digest step 3 names, that each kept page, each post
whose transcript is speech and each kept digest is unchanged, that each
restored page is its earlier copy byte for byte, that each merged page
differs from both copies, that each post whose transcript is not speech
holds none, that each revised digest was written again, that no file is
deleted, and that nothing outside the files this directive names has
changed. It records the directive only when every condition holds. Run it
even when you could not finish the steps above, and when it refuses, do
what its output says.

## 6. Ask again for each video no history holds

Once the directive is recorded, every `bin/dex enrich` command runs again.
For each video under **Videos no history holds**, run
`bin/dex enrich mark <url> queued`, with the video's address exactly as the
materials give it. The run then downloads it again where the address still
serves it, and it owes a description like any other download. This is the
one thing this directive fetches: the video itself, which nothing else can
give back.

## 7. Commit

Commit every file you changed, the ledger when step 6 changed it, and
`state/directives.jsonl` together as one commit. The subject is the first
line `bin/dex directive show 3` printed, `directive 3: ` followed by this
directive's intent. The body names every change, so the owner can find each
earlier copy:

```
Earlier copies: git show <earlier commit>:<earlier path>

Restored:
- <path>: <what the re-read lost>

Merged:
- <path>: <what each copy alone held>

Transcripts taken out:
- <path>: <what it was>

Digests revised:
- <item id>: <what changed>

Videos asked for again:
- <url>
```

Write the first line as it stands with the earlier commit's hash in place
of `<earlier commit>`, keeping `<earlier path>` as written. Leave out a list
that would be empty. When nothing changed, the body is one line saying so.
Write the message to `cache/directive-3-message.txt` and commit with
`git commit -F cache/directive-3-message.txt`, which keeps its quotes and
backticks exactly as written.
