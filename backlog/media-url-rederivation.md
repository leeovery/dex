# Media URL re-derivation — the picture the site moved, not lost

A media unit is seeded from the page's `og:image` at the moment the page
was fetched, and that URL is then the only thing the engine will ever ask
for. When a site redesigns, the asset moves and the stored URL becomes
permanently wrong — but the page usually still carries a perfectly good
`og:image`, one parent re-read away. Nothing re-derives it. The unit
retries five times against a URL that cannot work, escalates to `manual`,
and a session retires it by hand while the current image sits unfetched.

Three live cases from one instance, all seeded before v0.1.6 and surfaced
when migration 13 requeued them (probed 2026-09-11):

| stored media URL | what it answers today |
|---|---|
| `memgpt.ai/assets/img/memgpt-system-diagram.png` | 200, `text/html`, 22,739 bytes — redirects to `www.letta.com/` |
| `aitmpl.com/images/social-preview.png` | 200, `text/html`, 60,276 bytes — the SPA's index document |
| `terax.app/opengraph-image?…` | 200, `image/png`, **0 bytes** |

The first is the shape worth building for: MemGPT became Letta, the
project renamed and moved, and `letta.com` serves an `og:image` right
now. The evidence that the destination is worth reading is already on
disk — the migration-13 repair wrote the discarded HTML body's content
into that item's retired description, which is how we know the redirect
target is the live product page and not a parking page.

What makes this different from a dead link: the SUBJECT is alive and the
engine already knows where it lives. The parent page unit carries the
media unit's `parent` on the ledger, so the lineage needed to re-read is
recorded; only the decision to use it is missing.

Open questions, none of them settled:

- **What triggers a re-read.** Every blocked media attempt is too eager —
  a bot wall clears on its own and a re-read would waste the parent's
  fetch. The `manual` escalation at five attempts is the natural moment:
  the engine has already concluded the URL will not answer, and that is
  exactly when it currently gives up. Doing it there costs one page fetch
  per permanently-dead image, once.
- **Same unit or a new one.** Re-pointing the existing unit's `url` would
  break the hash that identifies it (`work_hash(url)`), and the ledger's
  identity rules do not bend. A child unit seeded `via: rederive` with
  the same `parent` and `depth` is the shape that fits, leaving the dead
  one terminal on the record — but then two units claim one media slot,
  and the slot rules would have to say which wins.
- **Whether a re-derived image is the same picture.** A site that
  redesigned may have changed its card entirely, so any standing
  description of the old image is stale by content, not just by name —
  the failure mode migration 14 exists to prevent. A re-derived unit
  landing in an occupied slot has to retire the old reading the same way
  a deletion does.
- **Scope beyond `og:image`.** The same argument covers any media URL a
  driver derived from a page rather than from the owner's own capture.
  Capture media is never re-derivable and must stay out of it.

Worth weighing against doing nothing: the cost today is one `manual` row
per moved image and a session's one-line skip, which is cheap and honest.
The case for building it is that the picture is recoverable and the
engine is throwing it away — and that every instance ingesting links to
young projects will hit this as those projects rebrand.

Recorded 2026-09-11, from the three units migration 13 requeued
(engine #146's neighbours — the descriptions were the defect, these URLs
were not).
