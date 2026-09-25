# Rehome the owner's CLAUDE.md

Until this engine release, each instance's CLAUDE.md was written by its
owner: a title, the import of the contract, a list of what the instance
covers, and sometimes more, such as the server and channels a Discord pull
exports. CLAUDE.md now belongs to the engine and is the same in every
instance, and `bin/dex sync` replaces this instance's copy once git history
holds it. The engine finds the owner's version in that history and prints
it for you. This directive gives every part of it a home: what the instance
reads for goes into `lens.md`, the Discord server and channels go into the
`discord` key of `state/config.json`, and whatever has no home is named in
the commit message, where the owner can find it again. It always
completes, including on an instance that has no scope to carry.

This directive edits `lens.md` and `state/config.json` and nothing else. It
is the engine's decision and runs unattended, with full authority over both
files, `state/config.json` included, whatever instructions you hold from an
earlier release say about editing config in an unattended run. Never edit
CLAUDE.md, which sync owns, or README.md, which directive 2 rewrites. The
engine reads the Discord exports under `raw/discord/` for you and lists
their ids in the materials.

## 1. Read the owner's CLAUDE.md from the materials

The materials are printed after these instructions, between the
`===== materials` line and the `===== end of materials =====` line. They
hold up to three things, each between delimiter lines of its own:

- The owner's CLAUDE.md, the newest committed version that is not an
  engine copy, between the
  `----- the owner's CLAUDE.md, from commit <hash> -----` line and the
  `----- end of the owner's CLAUDE.md -----` line. Note the hash, because
  the commit message names it. When this instance's history holds no
  version of the owner's, a line saying so stands in its place.
- When the owner's CLAUDE.md states a scope, the seed lens rendered for
  this instance, between the `----- the seed lens for this instance -----`
  line and the `----- end of the seed lens -----` line.
- The Discord exports, between the
  `----- the Discord exports under raw/discord/ -----` line and the
  `----- end of the Discord exports -----` line: one line for each
  directory under `raw/discord/`, giving its name and the `guild.id` and
  `channel.id` the exporter wrote at the top of its `messages.json`, or
  saying the export is unreadable and why. When there are no exports, a
  line saying so stands in their place. Step 4 works from this list.

There is no stated scope to carry when the materials say that no committed
version is the owner's, or that the owner's CLAUDE.md states no scope
because it still holds the old template's scope placeholders. The
directive completes all the same, and step 3 says what happens to the
lens.

## 2. Sort every passage

Skip this step when there is no owner's CLAUDE.md. Otherwise read it
whole, then sort every passage in it, each heading, paragraph, list and
list item, into exactly one of the kinds below. Sort by what a passage
says and never by its heading, because owners named and arranged their
sections freely.

- **Engine boilerplate.** The title's instance name and its
  `(a dex instance)` suffix, the paragraph saying this is a personal,
  LLM-maintained knowledge base that Claude operates as the application,
  and the `@.claude/dex-contract.md` import. The engine's CLAUDE.md carries
  all of it now, so it needs no home and no mention.
- **What the instance reads for.** The scope list, whatever its heading,
  with every parenthetical and every owner ruling inside its items. The
  domain phrase in the title, which is the words between the instance name
  (with the dash after it) and `(a dex instance)`, such as
  `Home Cooking Knowledge Base`. Any guidance on how to read this
  instance's content, such as what a community's shorthand means or which
  kinds of link are noise here. Subjects the owner named as out of scope,
  one by one, which are what this instance sets aside. All of it goes into
  `lens.md` word for word (step 3), except a template placeholder such as
  `<Domain>` or `<topic>`, which the owner never filled in and which is
  never carried.
- **The old door rule.** The blanket statement that whatever is not listed
  is out of scope, and any statement that a borderline find needs the
  owner's decision or that the list is mirrored in README.md and a change
  must update both files. The template worded it "Anything not listed is
  out of scope. When unsure, ask the owner rather than guessing. The list
  is mirrored in README.md", and owners often reworded it, as in "anything
  unrelated to cooking is out of scope" or "borderline, ask". The lens
  replaces the door, so remove these, never carry them into the lens, and
  name them in the commit message. One sentence often states both kinds,
  as in "Anything health-related is fair game; anything unrelated to
  health is out of scope.", where the half before the semicolon says what
  the instance reads for and the half after it is the door rule. Split
  such a sentence where its halves meet: the half that says what the
  instance reads for is carried into `lens.md` word for word under
  `## Reads for` (step 3), and only the fence half is removed and named in
  the commit message.
- **Routing to another instance.** Any sentence that sends some material
  to another instance or knowledge base, such as "that belongs in the
  other KB, not here". Remove it whole, the subjects it names included,
  never carry it into the lens, and name it in the commit message, because
  a lens never names or points to a sibling instance. When the routing is
  a clause inside a scope item, cut only that clause and carry the rest of
  the item word for word.
- **Discord facts.** The server's id, and the names and ids of the channels
  a pull exports. They become the `discord` key of `state/config.json`
  (step 4). The rest of a Discord section, such as how to run a pull, where
  the token lives, and notes about machinery still to come, is engine
  documentation now, in the dex-run skill's backfills reference: remove it
  and name it in the commit message. Never copy a token or any other secret
  anywhere, the commit message included.
- **Everything else.** A passage that is none of the above has no home
  here. Remove it and name it in the commit message.

## 3. Write the lens

When there is no stated scope to carry, write nothing to `lens.md`. When
it is missing, leave it missing: an instance with no lens is a general
knowledge dex, which reads everything shared into it for its general
substance, and that is a legitimate way to run one. When it exists, leave
it exactly as it is. Name in the commit message anything the owner's
CLAUDE.md says about what the instance reads for, such as reading
guidance, as not carried for that reason. Then go on to step 4.

When there is a stated scope, the seed lens in the materials is the
layout to start from. Its placeholder lines are the ones made only of text
in angle brackets, such as `<what to look at hardest>`. Read `lens.md` as
it stands and do exactly one of these:

- **It already states a lens**, because it exists, is not empty and holds
  none of the placeholder lines. Leave it exactly as it is and carry
  nothing into it. Name the owner's scope in the commit message as not
  carried, because `lens.md` already states the lens.
- **It is missing or empty.** Write it from the seed lens in the
  materials, filled in from the owner's CLAUDE.md:
  - Keep the seed's title line, `# ` followed by this instance's name.
  - As the first paragraph under the title, put the title's domain phrase
    word for word. With no domain phrase there is no such paragraph.
  - Under `## Reads for`, put the scope items word for word: each item
    exactly as it stands, with its wrapped lines, parentheticals and
    rulings, in the owner's order, and with any structure of its own
    (nested items, sub-headings, paragraphs) kept. The reads-for half of a
    sentence split in step 2 goes here too, word for word, in the place
    the sentence held.
  - Put reading guidance under `## Emphasise` when it says what to look at
    hardest, under `## Set aside` when it says what to ignore (the subjects
    named as out of scope go there too), and otherwise under a heading of
    its own after `## Reads for`, named for what it holds.
  - Delete every seed heading that nothing goes under, and every
    placeholder line.
- **It still holds placeholder lines** beside lines the owner wrote. Keep
  every line that is not a placeholder, fill each placeholder from the
  owner's CLAUDE.md as the case above does, and delete each placeholder
  line that nothing fills, with its heading when the heading is left
  empty.

## 4. Move the Discord facts into config

Skip this step when there is no owner's CLAUDE.md, or when it names no
Discord server or channel. An owner's CLAUDE.md that states no scope may
still name them, and they move all the same.

When `state/config.json` already has a `discord` key, leave it exactly as
it is, and name the Discord facts in the commit message as not carried,
because config already holds them. Otherwise add the key beside the file's
other keys, changing none of them, and create the file holding only this
key when it does not exist:

```json
"discord": {"guild": "<server id>", "channels": {"<channel name>": "<channel id>"}}
```

Every id is a string of digits in quotes, never a bare number. Work out the
values from the Discord exports in the materials, which hold every id the
exports carry, and never open an export yourself:

- **Each channel's name is the directory its export lands in**, because
  normalize derives item ids from `raw/discord/<name>/`, and a channel
  whose name changes has every conversation in it filed again as a new
  item. Each line of the Discord exports in the materials names one such
  directory, with the `guild.id` and `channel.id` its export carries.
- **A channel the owner's CLAUDE.md names that has an export** is the
  directory whose `channel.id` matches the id it gives, or, when it gives
  no id, the directory it names. The channel's name is exactly that
  directory's name, and its id is the export's `channel.id`. When the
  owner's CLAUDE.md disagrees with the export about either, the export
  wins, and the commit message says what differed.
- **A channel it names that has no export yet** takes the name it gives,
  without a leading `#`, and the id it gives. When it gives no id, leave
  the channel out and name it in the commit message.
- **An export it does not name** stays out of config. Name the export's
  directory in the commit message, so the owner can add it.
- **An export the materials list as unreadable** has no ids to go by, so a
  channel the owner's CLAUDE.md names matches it by the directory's name
  alone. That channel takes the directory's name and the id the owner's
  CLAUDE.md gives, and is left out when it gives none. Either way, name
  the export in the commit message as unreadable, with the reason the
  materials give.
- **The server id** is the one it gives. When the exports' `guild.id`
  differs, the exports win, and the commit message says so; when it gives
  none, the server id is the exports' `guild.id`. When it names more than
  one server, configure the one the exports come from, or the first it
  names when there are no exports, and name the others in the commit
  message.

When no server id can be found, or no channel is left to configure, add no
key, and name the Discord facts in the commit message as not carried, with
that reason.

## 5. Check and record

Read `lens.md` and `state/config.json` once more against steps 3 and 4,
then run `bin/dex directive done 1`. It confirms that `state/config.json`
parses under the engine's config rules and, when there is a stated scope
to carry, that `lens.md` states a lens: it exists, is not empty, and holds
none of the seed's placeholder lines. With no stated scope, `lens.md` is
no condition. It records the directive only when every condition holds.
Run it even when you could not finish the steps above, and when it
refuses, do what its output says.

## 6. Commit

Commit `lens.md` when you wrote it, `state/config.json` when you changed
it, and `state/directives.jsonl` together as one commit. The subject is
the first line `bin/dex directive show 1` printed, `directive 1: `
followed by this directive's intent. The body accounts for the owner's
CLAUDE.md, so the owner can find every part of it:

```
The owner's CLAUDE.md is in history: git show <hash>:CLAUDE.md

Moved:
- <passage>: <where it went>

Removed:
- <passage>: <why, in one line>
```

Name each passage by its heading, or by its first few words in quotes when
it has none, and never quote a secret, not even in part. Name the old door
rule and every routing sentence under Removed, with anything else not
carried, and say there when an export overrode the owner's CLAUDE.md. For
a sentence split in step 2, name its fence half under Removed in its own
words, and its reads-for half under Moved. Engine boilerplate needs no
line. Leave out a list that would be empty.

When there was no stated scope to carry, say so in a paragraph between the
history line and Moved: the instance had no stated scope, with why (its
scope still held the old template's placeholders), so it reads as general
knowledge, with no `lens.md`. When a `lens.md` was already there, say
instead that it was left as it was. When no committed version was the
owner's, there is no history line, and that paragraph is the whole body,
saying so.

Write the message to `cache/directive-1-message.txt` and commit with
`git commit -F cache/directive-1-message.txt`, which keeps its quotes and
backticks exactly as written.
