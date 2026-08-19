---
name: dex-query
description: Answer a question from this dex instance's knowledge. Use for "what do we know about X", "what's the current thinking on Y", recall questions like "what did I share about Z" or "what came in recently", or any knowledge question in the instance's domain.
---

# Query

Two modes. **Knowledge** ("what do we know about X") answers from the wiki.
**Recall** ("what did I share about X", "that article from last week", "what
came in this month") answers from the corpus: grep `state/digests/` and
`corpus/` by keyword and date, and reply with the item — its date, URL, the
owner's original note, key digest facts, and where the wiki filed it. The
corpus is the rolodex of everything ever shared; recall answers never depend
on the wiki. For "brief me on it", read the item's `enrichment/<id>/` — the
full fetched source — and summarize from that.

1. **Pull.** `git pull` before reading — another surface (a scheduled run,
   another machine) may have ingested since this folder last synced. If the
   pull fails, answer anyway and say the answer is as of the last local sync.
2. Read `wiki/index.md` first; open the relevant pages; follow `[[wikilinks]]`.
   For depth, read the cited items' digests (`state/digests/<id>.md`) and, when
   needed, their full sources (`enrichment/<id>/`).
3. **Recency rules**: newest material wins for "current state"; when items
   conflict across time, surface the conflict with dates — it is information.
   Cite item ids in backticks throughout.
4. Wiki thin or silent on the subject? Sweep `state/digests/` directly (grep by
   keywords) and say the answer came from digests rather than pages.
5. **Compound**: if the answer required real synthesis (not just reading one
   page), file it as `wiki/syntheses/<slug>.md` — frontmatter `type: synthesis`,
   `question:`, `generated:` date; body cites item ids and wikilinks related
   topics. Add to index, log it, commit and push. Syntheses are question-shaped, dated
   snapshots; the health check refreshes them as new material lands.
6. Never cite wiki pages as sources — item ids only.
