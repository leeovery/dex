# Heal the 0.2.2 damage through a directive

A directive, shipped in a release of its own, that asks each instance's
session to find what engine 0.2.2 damaged in that instance and repair it
from the instance's own git history, with judgment. No migration can make
these calls, and nobody here can inspect another owner's instance, so the
repair belongs to the one session that can read both copies side by side.

## What 0.2.2 left behind

- **Pages its re-read replaced.** Migration 18 queued a live re-fetch of
  every page the article seam had stored. 0.2.3's migration 19 cancelled
  the ones still pending; the ones that landed replaced the stored copy.
  Some re-reads are worse (code blocks flattened, notebooks stored as raw
  JSON, prose after a code block lost), and many are better. Counting fence
  lines and table rows cannot tell which: on a large instance, the
  re-reads holding fewer rows had dropped only separator lines, junk
  equation-number rows, or rows the live page no longer carried, and most
  held content the older copy lacked. Only a reading can decide.
- **Duplicate pictures.** Re-read pages re-emitted images whose CDN had
  re-signed or moved them, and the same bytes landed a second time in the
  next slot, owing a second description (#180). 0.2.4's media stage stops
  new ones; the copies already landed stay.
- **Videos and descriptions a transcript retired.** 0.2.3's migration 20
  restored every one git history holds; it names the rest in its report.
- **Digests drawn from any of the above.**

## Shape, as far as it has been thought through

- **Code finds, the session judges.** The directive's materials list, per
  instance, exactly what 0.2.2 touched, read from the ledger and git
  history: each replaced page with the commit holding the copy before it,
  each byte-identical media pair, each unit migration 20 could not
  restore. The session never goes hunting.
- **Nothing is re-fetched.** For each page the session compares the two
  copies and keeps the better one, or merges them; for each duplicate it
  keeps the first-landed file and moves a description over where the kept
  file has none. Digests drawn from a replaced copy are written again.
- **Descriptions come in many formats.** The describe verb's opens
  ``Describes `<name>` ``; older ones name their file in a heading, a line
  of prose or front matter, and they are common in older instances. Find
  them through `descriptions_of` in `pipeline/enrichment.py`, which reads
  every shape seen in the field; a description it cannot pair stays where
  it is.
- **It fits one run.** While a directive is pending, `dex-inbox`,
  `dex-normalize`, `dex-enrich` and `dex-exclude` refuse, so an open-ended
  directive stalls ingestion. Bound the work, or let the check pass on
  progress recorded per item.
- **Its check** has to be mechanical, like every directive's: what it can
  verify is that every listed item carries a recorded outcome, not that
  the outcome was right.

## When to pick it up

After 0.2.4. Before it ships, follow its instructions by hand on the
owner's own instances: that repairs them and tests the directive on real
data before any other instance runs it.
