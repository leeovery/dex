# Standing signals

They are counts, not a queue that drains. Nothing clears either one, so
the same numbers stand at the next check and a rising count is the only
news in them. Neither is a chore to work through, and neither fails the
check.

- **Stored threads recorded incomplete** — the walk-up stopped at a
  parent it could not fetch (`chain_incomplete`), or at the driver's
  100-hop bound (`thread_cap_hit`). The bound is a sanity stop against a
  cycle or a self-referencing parent, not an editorial one: 30-post
  threads walk clean, so a cap hit is about the chain being pathological,
  not about a root left behind. A dead parent stays dead, so these never
  go away; the listing is newest first, and the ones that matter are the
  items you are about to write about. When you use one, make the gap
  explicit: a digest bullet saying what is missing, and no page sentence
  that reads the stored chain as the whole thread.
- **Re-entry cap fires** — a tuning signal about the depth-4 / 12-URL
  bounds. Read the refused URLs: primary artifacts of the item's
  subject being turned away means the bounds are too tight for this
  corpus (raise it with the owner — the caps live in the engine);
  blogrolls and footers being turned away means harvest is promoting
  what the subject rule would not, and the fix is the judgment, not the
  bound. Concentrated in one or two items is normal; spread across many
  is the signal. Every promotion goes through `enrich fetch`, so this
  reading covers refusals whoever typed them — a link you promoted on
  the subject rule and a URL the owner named by hand say the same thing
  about the bounds. What leaves the reading is an **override**: a
  refusal answered with `--force` was a decision taken, not drift, and
  the count already excludes it. The listing is newest
  first, like the thread markers above it: the rows the report names are
  the most recent fires, and the older ones stand behind the count.
