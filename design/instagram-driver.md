# Instagram driver

Instagram posts shared to an instance's inbox become corpus items like any
other source: author, caption, and media, fetched mechanically and digested
by the session. The driver is **push-only and credential-free** — it fetches
what an anonymous visitor can see, through a third-party embed proxy, and
parks honestly when it cannot.

This is a **driver, not a watcher**. Nothing here polls Instagram, and
nothing here reads a saved collection. The watcher question is untouched by
this design and is not made harder by it: a watcher, if it is ever built,
only produces URLs, and those URLs still need exactly this driver to become
knowledge. Building the driver first is the sequencing that serves both.

Facts below marked **(verified 2026-08-26)** were established by testing
against live endpoints in the design session, not reasoned about.

## 1. Shape

```
  iOS share sheet ──▶ Send To Dex ──▶ inbox/<stamp>.md ──▶ InstagramDriver
   (URL only)         (unchanged)     (URL in body)              │
                                                                 ▼
                                                    embed proxy (anonymous)
                                                                 │
                                            ┌────────────────────┴───────┐
                                            ▼                            ▼
                                     og: meta tags                  media files
                                  author + caption            /images/…  /videos/…
                                            │                            │
                                            ▼                            ▼
                                       Content.body            images kept as media
                                                               video → transcribe,
                                                                 binary discarded
```

No shortcut change. No stored credential. No Instagram account involved at
any point — not the owner's, not a burner's, not the proxy operator's. The
proxy fetches public pages as a logged-out visitor.

## 2. Identity and canonicalization

**A post's identity is its shortcode.** Instagram serves one post under
several path shapes, and they are interchangeable: `/p/<code>/`,
`/reel/<code>/`, `/tv/<code>/`, and the username-prefixed
`/<user>/p/<code>/`. Fetching `/reel/<code>/` and `/p/<code>/` for the same
code returned byte-identical metadata **(verified 2026-08-26)**, so one post
is one work unit however it was shared — the same rule `XDriver` applies to
X's five status-URL spellings.

Canonical form is `https://www.instagram.com/p/<code>/`. Fetches address the
proxy by the `/p/<code>/` path, the shape it serves for all of them.

**Two shortcode grammars exist in the wild, and only one works.** An
ordinary share yields an 11-character code (`DcgAEpxCBDL`). A share of a
restricted post yields a ~39-character share token
(`DXRrALviA5hPH4K9L8-2lwQGcLoHWMFKOnkw6E0`) **(verified 2026-08-26)**. The
proxy handles the short form and cannot resolve the long one — it redirects
the request back to `instagram.com` rather than erroring. The token grants
no anonymous access: it is a share link, not a capability.

The driver therefore matches both grammars (so a long-token URL is *owned*
rather than falling through to the web catch-all, which would fetch
Instagram's app shell and ledger garbage) and treats an unresolvable long
token as the park in §6. Whether a long token ever accompanies a *public*
post is **unverified** — if it does, the driver must resolve it to its short
form before fetching, and it will otherwise look like a random failure.
Check this against more shares before shipping.

## 3. Transport: the embed proxy

The proxy is a small server that fetches a public Instagram page and
re-serves author, caption and media pointers as OpenGraph meta tags. It was
built so Discord and Telegram could render Instagram previews.

**The proxy answers bots, not browsers (verified 2026-08-26).** A normal
browser User-Agent is redirected straight to `instagram.com`; the embed data
is served only to a bot UA. The driver must send one deliberately. This is
the single non-obvious fact about the transport and the reason a first test
looks like total failure.

Instance survey **(verified 2026-08-26)**:

| host | result |
|---|---|
| `uuinstagram.com` | author, full caption, indexed media — the best of the four |
| `instagramfix.com` | author and media, but an **empty caption** on a post `uuinstagram` captioned |
| `kkinstagram.com` | redirects to the raw CDN image; no metadata at all |
| `ddinstagram.com` | **dead** — DNS does not resolve |

Media is addressable at `/images/<code>/<n>` and `/videos/<code>/<n>`, and
serves real bytes: the tested reel returned a valid 10.87 MB MP4. **An
out-of-range index returns HTTP 200 with a zero-byte body, not a 404**, so
carousel length must be probed by content length, never by status code —
this is the trap in the transport.

**There is no JSON route.** `/json`, `/api/…` and `/embed` all return HTML
**(verified 2026-08-26)**. The driver parses meta tags. This is strictly
worse than the `api.fxtwitter.com` JSON that `XDriver` enjoys, and it is the
cost of the source, not a shortcut taken here.

**The base URL is instance config, not a constant** — the same shape
`transcribe_base_url` already takes. The lineage is visibly decaying:
upstream InstaFix was archived 2 April 2026, `ddinstagram.com` has already
gone, and the surviving hosts run unmaintained code. Configurability makes
switching hosts (or self-hosting) a config change rather than a release.

### What the proxy costs

Recorded so the trade is not rediscovered later. None of these are account
risk — no account exists in this path:

- **A third-party dependency that is decaying**, per above.
- **The operator sees the instance's lookups.** Every fetch tells that
  server which posts this knowledge base is interested in. For a system
  whose purpose is recording what its owner attends to, this is the
  sharpest cost, and it is the main argument for self-hosting.
- **Its output is trusted verbatim.** Whatever it returns is written into
  the corpus as fact.
- **No SLA and unknown rate limits.**

Self-hosting (Go, Dockerised, a maintained fork exists) fixes the first
three and buys a datacenter IP that Instagram blocks faster than a
residential one, plus the maintenance of archived code. Not chosen for v1;
the configurable base URL is what keeps the choice cheap.

## 4. What the driver returns

Mapped onto `Content`:

- **`body`** — the caption, attributed to the author handle taken from
  `og:title` / `twitter:title`. A post whose caption is empty and which
  carries media is `done` with a minimal attributed body, matching the X
  driver's rule; "no text or media" is said only when it is true.
- **`media`** — absolute proxy URLs for images. Video is fetched for
  transcription and not kept (§5).
- **`meta`** — author handle, shortcode, canonical Instagram URL, and which
  proxy host answered (so a bad host's output is traceable after the fact).

Engagement counts stay unrecorded, per the standing rule — they are
snapshot noise.

## 5. Media policy: video transcribes, images are kept

**Instagram video is transcribed and the binary is dropped. Images are
kept.** This is a driver-level rule, not instance config — no new knob.

The reasoning is that a knowledge base stores knowledge, not artifacts. A
reel's value is what is said in it; the transcript is that, and the original
stays one click away at the canonical Instagram URL. Keeping the MP4 buys
re-watching, which is not what the corpus is for, and it costs LFS budget
against a 1 GB free tier.

Images are the exception because for an image the pixels *are* the
knowledge — a described design is not a substitute for the design. That
keeps a design-oriented instance correct without a per-instance setting.

**The discard behaviour already exists — it is the transcribe lifecycle, and
this driver joins it rather than inventing anything.** A video post is not
`Content` with a media URL; it is `NeedsCapability(need=Need.TRANSCRIBE)`,
exactly as the podcast driver returns for an episode enclosure. The run
layer then acquires the audio into gitignored `cache/audio/<hash>.<ext>`,
transcribes it, writes the transcript as the unit's output, and **deletes
the audio on success only** — pending or failed audio stays cached so a
retry does not re-download. Nothing ever reaches `enrichment/<id>/` or LFS.

Two consequences worth stating plainly, because they correct the shape this
design was first drafted in:

- **No media-ceiling change is needed for video.** The 10 MB per-file
  ceiling governs the media stage, which handles `Content.media` downloads.
  Transcribe acquisition is a different path with its own rules, so the
  10.87 MB reel **(verified 2026-08-26)** never meets that ceiling at all.
  The ceiling still governs images, where it is not a constraint in
  practice.
- **An Instagram video URL is shaped exactly like a podcast enclosure** — a
  direct MP4 over HTTP. The existing enclosure download is the model, and
  probably the code.

The caption rides on the `NeedsCapability` as its `body`, the way the
podcast driver carries show notes, and is composed with the transcript when
the transcript lands.

**This is the one place the driver touches shared machinery.** Audio
acquisition and transcript-body composition both dispatch on `entry.kind`,
and the run layer raises loudly if a kind appears in one and not the other —
"`_acquire_audio` and the body dispatch must cover the same kinds". Adding
`Kind.INSTAGRAM` means adding it to both, deliberately and together.

If that coupling is judged too invasive for a first cut, the smaller v1 is a
video post returning `Content` with the caption and no media at all — the
binary dropped, the canonical link preserved, transcription following in a
second step. That is a real option, not a fallback: it keeps v1 entirely
inside `drivers/`.

## 6. Failure: private accounts are out of scope, and say so

**A private-account post yields nothing and the signal is clean.** The page
returns a bare `<title>Instagram</title>` with **zero `og:` tags** — no
title, no description, no image — and the proxy redirects the request to
`instagram.com` rather than serving data **(verified 2026-08-26,
against a real private post)**. A public post by contrast returns author,
full caption and a media pointer.

Two reliable detections, then: the proxy redirecting to an `instagram.com`
host, and the total absence of `og:` metadata.

The driver parks these `manual` with a reason that names the cause and the
way through — *private or unavailable; screenshot it and share that if you
want it*. This is deliberately not a generic fetch failure and deliberately
not `dead`: the content exists, the owner can see it, and only this
transport cannot. Condemning it would be the terminal-mislabel class the
pipeline design exists to prevent.

**Why the gap is acceptable.** Shareable content comes from accounts that
want to be found, and being private cuts directly against that. Businesses,
influencers and creators publish publicly by design; private accounts are
personal. The gap is real but sits almost entirely outside the content
anyone would capture into a knowledge base.

**The manual fallback needs no machinery.** Screenshot the post and share
the image: binary capture already stages it under `media/<id>/`, and the
cognitive OCR floor already reads it at ingest.

## 7. Config

- `instagram_base_url` — the embed proxy host. Defaults to the surviving
  best-metadata instance; overridable per instance, and the escape hatch
  when a host dies or the owner self-hosts.

Nothing else. No credentials, no provider list.

## 8. What this design rejects, and why

Recorded because each was investigated properly and the reasons will
otherwise be relitigated.

- **Official Meta APIs.** Graph API serves only your own Business/Creator
  account's own posts; Basic Display API was shut down; oEmbed was
  deprecated 1 October 2025 and since 3 November 2025 drops author and
  thumbnail data. Nothing official reaches an arbitrary public post, and
  nothing official has ever reached saved collections. An Instagram
  spokesperson states there is no data portability tool; the Download Your
  Information export covers content you created, not posts you bookmarked.
- **instaloader** — healthy (v4.15.3, last commit 26 July 2026) and capable,
  but authenticates as the owner, which is account risk. Separately it
  **cannot filter by collection** (open request #2073, blocked on Instagram's
  current GraphQL `doc_id`), so it would not deliver collection→instance
  routing even if the risk were accepted.
- **instagrapi** — does collections properly (`collection_pk_by_name` →
  `collection_medias`) but impersonates the *phone app's* private endpoints,
  which is the highest-detection surface Instagram polices. The thing most
  wanted sits behind the riskiest option.
- **Commercial scraping APIs** (Bright Data, Apify, HikerAPI, SocialCrawl).
  All public-data-only. What they sell is *anonymity at scale* — residential
  and mobile proxy rotation, TLS fingerprint spoofing — against Instagram's
  internal endpoints. **Nobody sells private access, structurally**: seeing a
  private account requires being an approved follower of that specific
  account, granted per-target by a human, which no proxy farm can fake and
  no vendor can resell.
- **Share-time pixel capture.** Dead: the Instagram share sheet hands over a
  URL and nothing else — no image representation **(verified 2026-08-26 via
  a share-sheet inspector shortcut)**. A screenshot must be a separate
  deliberate act.
- **Browser automation over an attached session** (Playwright
  `connect_over_cdp` against the owner's already-running, already-logged-in
  browser). Genuinely viable, and philosophically consistent with "auth is
  borrowed, never stored" — it borrows the browser's session exactly as the
  engine borrows `gh`'s token. It would serve push and pull with one
  mechanism and delete the proxy dependency entirely. **Rejected for v1 on
  residual risk**: fingerprints pass, but behavioural detection (regular
  pagination, no pointer movement) remains, and the owner declined any
  non-zero account exposure. Recorded here because it is the strongest
  option if that position ever changes, and because it generalises to every
  auth-walled source (X bookmarks without the paid tier, LinkedIn saves).
  If pursued: use the browser as an authenticated transport issuing
  same-origin `fetch()` against Instagram's internal endpoints, never DOM
  scraping — the markup is deliberately obfuscated and rotates.
- **Browser extensions** that export saved posts and collections locally
  (they exist and work, using the owner's own session with no server
  involved). Not engine machinery, but relevant later: an export dropped
  into a watched folder reaches dex through a generic filesystem watcher,
  giving private-collection content a path with no engine risk at all.

## 9. Implementation plan

Ordered; each step is independently reviewable.

1. **`Kind.INSTAGRAM`** in `pipeline/types.py`. New ledger and corpus
   vocabulary, so `instance/skills/dex-run/references/schema.md` changes in
   the same commit. No migration — a new kind moves no existing state.
2. **`drivers/instagram.py`** — `matches`, `canonical`, `fetch`, against the
   `SourceDriver` protocol. Both shortcode grammars in the URL regex; the
   bot User-Agent as a named module constant with the reason inline (it
   reads as a mistake otherwise); `sleep` set for politeness against a free
   community service. HTTP through `drivers/transport.py`, the existing seam.
3. **Meta-tag parse** — author, caption, media pointers; HTML entities
   decoded (`&#39;` appeared in real captions). Media URLs absolutized
   against the configured base and screened before emit: the media stage
   fetches verbatim and refuses repaired guesses.
4. **Carousel probing** — walk `/images/<code>/<n>` until a zero-byte body,
   never until a 404, and bound it by the media stage's file cap.
5. **Failure classification** — the `og:`-less page and the
   redirect-to-instagram.com both park `manual` with the private/unavailable
   reason. Nothing here may produce `Missing`; a private post is not gone.
6. **Registry** — insert into `registry.build_drivers` ahead of `WebDriver`,
   which stays last as the catch-all.
7. **Config** — `instagram_base_url` on `Config`, plus its row in
   `state-formats.md`.
8. **Transcription wiring** — a video post returns
   `NeedsCapability(need=Need.TRANSCRIBE)` carrying the caption as `body`.
   Add `Kind.INSTAGRAM` to audio acquisition (modelled on the podcast
   enclosure download — the video URL is a direct MP4) **and** to the
   transcript-body dispatch, together: the run layer raises loudly when a
   kind appears in one and not the other. This is the only step that leaves
   `drivers/`, and the one most likely to need its own review. Deferrable —
   see §5's smaller v1.
9. **Tests** — canonicalization across all four path shapes and both
   shortcode grammars; the bot-UA requirement; meta parsing including the
   empty-caption post; zero-byte carousel termination; the private-post park.
   Fixtures captured from the real responses recorded in this document.
10. **A live check**, marked `ci_hostile` — the proxy is an unmaintained
    third-party freebie that can rate-limit or vanish, and a daily failure
    mail on someone else's uptime is how a real alarm becomes noise.
11. **Housekeeping** — delete `backlog/instagram-driver.md` and its
    `backlog/index.md` line (the graduation convention); add `instagram` to
    the kind vocabulary in `schema.md` (the inline
    `youtube | x | github | …` list); and extend the README's `dex-enrich
    run` row, which enumerates what the drivers fetch.

## 10. Open, deliberately

- Whether a long share token ever accompanies a public post (§2). Cheap to
  settle with a few more shares; decides whether canonicalization needs a
  resolve step.
- Which proxy host ships as the default, pending a caption-reliability
  comparison across more posts than the two tested.
- **A post carrying both video and images.** A driver returns one outcome,
  and video means `NeedsCapability` while images mean `Content.media` — so a
  mixed carousel cannot be both. Likely resolution: video wins and the
  images are dropped, since a carousel mixing the two is rare and the
  transcript is the substance. Unverified that Instagram even produces this
  shape through the proxy's indexed media paths.
- Whether transcription ships in v1 or a second step (§5).
