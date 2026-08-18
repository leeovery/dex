---
name: dex-query
description: Answer a question from this dex instance's knowledge. Use for "what do we know about X", "what's the current thinking on Y", or any knowledge question in the instance's domain.
---

# Query

1. Read `wiki/index.md` first; open the relevant pages; follow `[[wikilinks]]`.
   For depth, read the cited items' digests (`state/digests/<id>.md`) and, when
   needed, their full sources (`enrichment/<id>/`).
2. **Recency rules**: newest material wins for "current state"; when items
   conflict across time, surface the conflict with dates — it is information.
   Cite item ids in backticks throughout.
3. Wiki thin or silent on the subject? Sweep `state/digests/` directly (grep by
   keywords) and say the answer came from digests rather than pages.
4. **Compound**: if the answer required real synthesis (not just reading one
   page), file it as `wiki/syntheses/<slug>.md` — frontmatter `type: synthesis`,
   `question:`, `generated:` date; body cites item ids and wikilinks related
   topics. Add to index, log it, commit. Syntheses are question-shaped, dated
   snapshots; the health check refreshes them as new material lands.
5. Never cite wiki pages as sources — item ids only.
