# Report surfaces

Status: **in force**. Every named surface in `render/surfaces.py` renders
markdown. This document is the vocabulary and the layout rules those
renderers implement; `design/ingestion-pipeline.md` §11 states the
architecture (judgment decides, code renders), and this states what the
rendering looks like and why.

## Who reads these

The primary reader is the Claude session driving a scheduled run. It reads
the report to decide what to do next, and it reads every line of it. The
owner reads the same reports rarely, in whatever client he happens to be in.

The surfaces were originally laid out for a person at a 72-column terminal:
fixed-width tables, column fitting, hard wrapping, and middle-elision of any
cell too wide for its column. Measured against real instance data that
layout destroyed the data the reports exist to carry. In one table 71-79% of
item ids were elided and in another 100% were; across 2452 real URLs, 106
rendered identically to a different URL, including four corpus items holding
two of their own URLs that collapsed onto each other. A report that renders
two different rows the same way is worse than no report.

## Decisions

**Markdown, not fixed-width layout.** Every surface emits markdown. No
column arithmetic, no padding, no rules, no box glyphs.

**Headings.** `##` opens the report and names it plus its scale
(`## Enrich run — 6 units processed`). `###` opens each section and names
the section plus its scale (`### Needs you — 1 entry the engine has given up
on`). A reader who stops at the headings still knows what happened and how
much of it there is.

**Bullets.** `-` for every list. One entry per bullet. A detail hanging off
an entry is a continuation line indented two spaces and opened with `↳`;
several details are several `↳` lines. Nested `-` bullets are for genuine
nesting (a migration's actions inside the migration), not for the details of
a single entry.

**Bold for identifiers.** Item ids, page names, URLs standing as an entry's
identity, and the count in a finding are `**bold**`. The bold is what the
eye lands on and what a grep for an id finds.

**Backticks for statuses and literals.** A ledger status (`manual`,
`blocked`), a capability (`transcribe`), a path or filename, a wikilink
target, a command. Backticked text is a value the reader can copy, type, or
match; it is never prose.

**Identity is never truncated and never wrapped.** An item id, a URL, or a
file path renders whole, on its own line, whatever its length. Real item ids
average 58 characters and one real URL in a live instance is 375. Nothing in
the render path may shorten one, split one, or elide the middle of one. The
elision code that could do so is deleted rather than bounded, because a
bound is a setting and a deletion is a guarantee.

**Prose may soft-wrap.** No renderer hard-wraps anything. Terminals,
editors and chat clients all soft-wrap, and a hard wrap inserted at render
time is a newline that is wrong in every viewport but one, and that a reader
searching for a phrase cannot match across.

**No tables.** Not a markdown table, not an aligned one. The only exception
is a genuinely short columnar count where every cell is a short label and a
small integer, and even those render as bullets or as one `·`-joined line
(`**done** 4 · **waiting** 1 · **blocked** 1`) rather than as a table.

**Sections group by who owns the next action, not by internal status.** The
old enrich report had one `parked` block holding four ledger statuses,
because that is how the ledger stores them. A reader does not need to know
that: he needs to know which entries wait on him and which the engine will
retry without being asked. So `manual` (the engine has given up) becomes
**Needs you**, while `blocked`, `waiting` and `error` (the engine retries by
itself) become **Waiting on the engine**, with the retry state visible on
the entry. The same rule renames every other engine-internal label to what
the reader must do about it.

**A marker rides inside the bullet text, never in the prefix.** A restated
fact is `- ~ the fact`, not a `~` gutter applied to the entry. This is the
one behaviour kept from the superseded wrapping work: a prefix repeats on
every continuation line, so two facts rendered four markers.

**Absent sections are absent; present checks state "none".** The two kinds
of report differ. A run report (enrich, sync, receipt) omits a section with
nothing in it, and says in one line when nothing at all happened; a list of
headings each reading "none" is exactly the noise markdown removes. A check
report (health) keeps one bullet per check even at zero, because there the
absence is the finding and its presence proves the check ran.

**A capped listing says it is capped.** A findings list showing only its
first N entries ends with a bullet naming how many it did not show. A
silently short list is a lie about scale.

## Vocabulary

| Reader-facing label | What it holds |
| --- | --- |
| **Needs writing up** | items with new enrichment for the session to digest |
| **Read these yourself** | jobs resolving to the cognitive floor (OCR, extraction Claude must do with eyes) |
| **Needs you** | entries the engine has given up on: ledger status `manual` |
| **Waiting on the engine** | entries the engine retries unasked: `blocked`, `waiting`, `error` |
| **Not finished** | items still `raw` because a unit they own has not landed |
| **Digest these** | items whose enrichment is newer than their digest |
| **Waiting on a capability** | the `waiting` cohort, counted by the capability it needs |
| **Repair with judgment** | a migration's skipped records |
| **REVIEW REQUIRED** | a migration's anomalies |

(The one table here is documentation, not a rendered surface.)

Detail text says what changed in plain terms. "3 new enrichment files, 1
rewritten" replaces "3 new, 1 changed", which never said new *what*.

## What the kernel is now

`render/kernel.py` existed for terminal geometry: display-column
measurement, greedy word wrap, hanging-indent wrap, fill-to-width, table
column fitting, key-value blocks, tree gutters, and middle-elision. Markdown
needs none of it and all of it is gone. What remains is composition of
markdown fragments: a heading, a bullet, a detail line, a `·`-joined inline
list, and the pluralizer that makes a heading's scale read as English. The
kernel keeps its old contract of holding zero dex vocabulary.
