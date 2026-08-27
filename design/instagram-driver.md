# Instagram driver

Instagram posts shared to an instance's inbox become corpus items like any
other source: author, caption, and media, fetched mechanically and digested
by the session. The driver is **push-only and credential-free** — it fetches
what an anonymous visitor can see and parks honestly when it cannot.

This is a **driver, not a watcher**. Nothing here polls Instagram, and
nothing here reads a saved collection. The watcher question is untouched by
this design and is not made harder by it: a watcher, if it is ever built,
only produces URLs, and those URLs still need exactly this driver to become
knowledge. Building the driver first is the sequencing that serves both.

Facts marked **(verified 2026-08-26)** were established by testing live
endpoints in the design session; facts marked **(verified 2026-08-27)**
in the follow-up session that resolved this design's gaps against the
engine code. Neither set is reasoned about — all of it ran.

## 1. Shape

Two anonymous sources, split by what each is good at **(verified
2026-08-27)**:

- **instagram.com itself, fetched with a bot User-Agent**, serves full
  `og:` metadata for a public post: the complete caption (untruncated,
  line breaks intact), the author's display name and handle, the post
  date, and a `rel="canonical"` link naming the shortcode. It serves a
  bot UA the way it serves a chat app's link-preview fetcher, because
  that is what it thinks is asking. It does NOT serve usable media: one
  signed cover-image URL, no video even for a reel.
- **The embed proxy** serves media and nothing we still need besides:
  `/images/<code>/<n>` and `/videos/<code>/<n>` answer **any** UA with a
  302 to the signed CDN URL, and the CDN serves real bytes to any UA.
  Its page endpoint (the bot-only one) truncates captions at ~255
  characters, so the page endpoint is not used at all.

```
  iOS share sheet ──▶ Send To Dex ──▶ inbox/<stamp>.md ──▶ InstagramDriver
   (URL only)         (unchanged)     (URL in body)              │
                                          ┌──────────────────────┤
                                          ▼                      ▼
                              instagram.com (bot UA)     proxy probe walk
                              caption·author·date·       /videos/<code>/<n>
                              canonical·public? ─ og:    typed per index
                                          │                      │
                                          ▼                      ▼
                                    Content.body      images → Content.media
                                                      video  → NeedsCapability
                                                        (transcribe, binary
                                                         discarded after)
```

No shortcut change. No stored credential. No Instagram account involved at
any point — not the owner's, not a burner's, not the proxy operator's.
Both fetches are what an anonymous link preview does.

## 2. Identity and canonicalization

**A post's identity is its shortcode.** Instagram serves one post under
several path shapes, and they are interchangeable: `/p/<code>/`,
`/reel/<code>/`, `/tv/<code>/`, the username-prefixed `/<user>/p/<code>/`
**(verified 2026-08-26)** and `/<user>/reel/<code>/` **(verified
2026-08-27** — natgeo's canonical reel URLs use it**)**. One post is one
work unit however it was shared — the same rule `XDriver` applies to X's
five status-URL spellings.

Canonical form is `https://www.instagram.com/p/<code>/`.

**Two shortcode grammars exist in the wild.** An ordinary share yields an
11-character code (`DcgAEpxCBDL`). A share of a restricted post yields a
~39-character share token (`DXRrALviA5hPH4K9L8-2lwQGcLoHWMFKOnkw6E0`)
**(verified 2026-08-26)**. The driver matches both, so a long-token URL is
*owned* rather than falling through to the web catch-all.

**Long tokens resolve at fetch time, not canonicalization time.**
`canonical` is offline (it keys the ledger during normalize, no network),
so a long-token URL canonicalizes to itself. The fetch goes to
instagram.com like any other: a public post answers with `og:` metadata
and a `rel="canonical"` link carrying the short code **(mechanism
verified 2026-08-27** against short-code pages; that a token page carries
it too is unverified until a real share token is fetched**)** — the
resolved code is what the media probe uses, and both spellings land in
meta. A token whose page comes back `og:`-less is the private park in §6.
Cost of the offline canonical: a post shared once by token and once by
code makes two units. Accepted — rare, and the alternative is network in
`canonical`, which nothing in the pipeline permits.

**The driver owns the whole host.** `matches` claims `instagram.com`,
`www.`, and `m.` — post URLs and everything else alike — because the
alternative is the web catch-all fetching Instagram's app shell and
ledgering garbage. Non-post shapes state what they are: `/stories/…`
parks `manual` (stories are login-walled and expire; *screenshot it and
share that*), profile roots and explore/share pages return `Unusable`
with the evidence naming the shape.

## 3. Transport

**Both metadata sources answer bots, not browsers.** The proxy's page
endpoint 302s a browser UA to `instagram.com` and serves a bot UA the
embed page **(verified 2026-08-26, redirect shape re-verified
2026-08-27: a plain 302 with a Location header)**. instagram.com serves a
bot UA full `og:` metadata where a browser UA gets the JS app shell
**(verified 2026-08-27)**. The driver therefore fetches pages with a
deliberate bot UA — the single non-obvious fact about the transport, and
the reason a first test looks like total failure.

**The seam gains a UA, not a redesign.** `drivers/transport.py` keeps its
protocol (`(url, *, method, limit)`) and grows a second exported
transport with the bot UA beside `urllib_transport` — same
implementation, one different header. The driver injects it as its
default the way every driver injects its transport; every other caller of
the seam is untouched. Verified untouched, not assumed **(all 2026-08-27,
against live endpoints)**:

- The media stage's browser-UA GET of `/images/<code>/<n>` follows the
  302 to the CDN and lands real bytes (a valid progressive JPEG).
- The transcribe acquisition's browser-UA GET of `/videos/<code>/<n>`
  lands a valid MP4 the same way; range requests answer 206.
- The media stage's HEAD oversize probe gets a 405 from the proxy, which
  the existing code already treats as inconclusive and settles by GET.
- No redirect detection is needed anywhere, so `HttpResponse` does not
  grow a final-URL field: every park decision reads the *body* (§6).

**The probe walk is the media enumeration.** There is no JSON route
**(verified 2026-08-26)** and the proxy's own page metadata lies about
media type: an all-image carousel is served `twitter:card: player` and
`og:video` pointing at a URL that 302s to a *JPEG* **(verified
2026-08-27**, natgeo two-image carousel**)**. What tells the truth is the
media endpoint itself, one index at a time:

| probe `GET /videos/<code>/<n>`, `limit=0` | meaning |
|---|---|
| 302 → CDN `.mp4` (content type `video/mp4`) | item *n* is a video |
| 302 → CDN `.jpg` (content type `image/jpeg`) | item *n* is an image |
| 200, zero-byte body, `Content-Length: 0` | past the end of the post |

**(all three shapes verified 2026-08-27.)** Out-of-range is a 200, never
a 404 **(verified 2026-08-26)** — status codes cannot terminate the walk.
The walk runs indexes 1..5 (the media stage's 4-file cap plus one, so a
truncation is visible), stops at the first past-the-end answer, and paces
1s between probes — the X driver's hop-pacing courtesy, same reason. Each
probe costs one byte of body (`limit=0` reads at most one).

**The proxy base URL is instance config, not a constant** — the same
shape `transcribe_base_url` takes. The lineage is visibly decaying:
upstream InstaFix was archived April 2026, `ddinstagram.com` is already
dead, and the surviving hosts run unmaintained code. Since captions no
longer come from the proxy, the host criterion is media-endpoint
reliability alone; `uuinstagram.com` ships as the default.

### What the two sources cost

Recorded so the trade is not rediscovered. None of it is account risk —
no account exists in this path:

- **The proxy operator sees which posts the instance fetches media
  for.** Sharper before, when it saw every lookup; still the argument
  for self-hosting (a maintained InstaFix fork exists, Dockerised).
  The configurable base URL keeps that a config change.
- **instagram.com sees an anonymous bot-UA page fetch per captured
  post** — indistinguishable from a link preview, but it is direct
  traffic, and whether Instagram answers a datacenter IP or a
  high-volume IP the same way is unknown. The live check will say
  (`ci_hostile`, §9).
- **Both outputs are trusted verbatim** into the corpus.
- **No SLA and unknown rate limits, on either.**

## 4. What the driver returns

Mapped onto `Content`:

- **`body`** — the full caption from instagram.com's `og:title`
  (attributed, untruncated, line breaks intact — the parse must span
  newlines inside the attribute value, which real captions carry
  **(verified 2026-08-27)**). A post whose caption is empty and which
  carries media is `done` with a minimal attributed body, matching the X
  driver's rule; "no text or media" is said only when it is true.
- **`media`** — absolute **proxy** URLs (`<base>/images/<code>/<n>`) for
  the indexes the probe typed as images. Proxy URLs, not the CDN URLs
  they 302 to: CDN URLs are signed and expire, and a ledger identity
  must not. Video is transcribed, not kept (§5).
- **`meta`** — author handle and display name, post date (instagram.com's
  `og:description` carries both, `handle on Month D, YYYY`
  **(verified 2026-08-27)**), shortcode, canonical Instagram URL, the
  share-token spelling when one resolved (§2), which proxy host served
  the media, and `images_dropped: n` when §5's video-wins rule dropped
  any. Engagement counts stay unrecorded, per the standing rule — they
  are snapshot noise, and the parse that finds the handle discards them.

## 5. Media policy: video transcribes, images are kept

**Instagram video is transcribed and the binary is dropped. Images are
kept.** This is a driver-level rule, not instance config — no new knob.

The reasoning is that a knowledge base stores knowledge, not artifacts. A
reel's value is what is said in it; the transcript is that, and the
original stays one click away at the canonical Instagram URL. Keeping the
MP4 buys re-watching, which is not what the corpus is for, and it costs
LFS budget against a 1 GB free tier. Images are the exception because for
an image the pixels *are* the knowledge — a described design is not a
substitute for the design.

**The discard behaviour is the transcribe lifecycle, joined, not
invented.** A video post is `NeedsCapability(need=Need.TRANSCRIBE)`,
exactly as the podcast driver returns for an episode enclosure — and the
video URL rides the outcome's meta under the **`enclosure` key**, because
that key is both what the acquisition reads back out of the park file's
frontmatter and the condition under which the run layer writes the park
file at all: a differently-named key on a caption-less reel would write no
file, and the acquisition would loop manual on its absence. The caption
rides as `body`, the way the podcast driver carries show notes, and the
transcript composes onto it when it lands. The run layer acquires the MP4
into gitignored `cache/audio/`, transcribes, and deletes on success only.
Nothing ever reaches `enrichment/<id>/` as video bytes or LFS.

The engine machinery this touches, all verified against the code
**(2026-08-27)**:

- **`whisper-local` decodes any container PyAV decodes** — the MP4 goes
  in as-is, no transcode step, and a video-only file with no audio
  stream already maps to a clean `ProviderInputError` → manual.
  `whisper-api` transcodes through ffmpeg and is equally content-shaped.
- **No media-ceiling change.** The 10 MB ceiling governs the media
  stage's image downloads; transcribe acquisition is a different path.
- **Three dispatch sites, not two.** `Kind.INSTAGRAM` joins
  `_acquire_audio` and the transcript-body dispatch (which raise loudly
  when they disagree) — **and** `_apply_acquisition_failure`'s
  podcast-only arm that maps a dead enclosure to `manual` with requeue
  advice instead of `dead`: a proxy `/videos/` 404 says nothing about
  the reel, and the generic arm would condemn it. The terminal-mislabel
  class, one `if` away.
- **A silent reel is routine, not an edge.** Music-only and ambient
  video is a large share of Instagram, and "whisper heard no speech"
  parks manual. The park reason must say what the session should do:
  *no speech to transcribe — the caption is already stored; mark done to
  keep it as the record, or rescue by hand.* For podcast and YouTube
  this case is rare noise; here it is a standing disposition.

**A mixed carousel: video wins, images are dropped and counted.** One
fetch returns one outcome, and `NeedsCapability` carries no media — so a
post whose probe walk finds any video item returns the transcribe park,
records `images_dropped: n` in meta, and the session's judgment can
rescue the stills (screenshot route, §6) when they matter. The probe
typing makes this rule safe where the page metadata made it wrong: the
all-image carousel that *claims* to be video **(§3, verified
2026-08-27)** probes as images and keeps them all. Whether a genuinely
mixed carousel occurs in the wild is still unobserved; the rule covers it
either way.

## 6. Failure: private accounts are out of scope, and say so

**The public/private signal is the instagram.com page itself.** A private
or unavailable post yields a bare `<title>Instagram</title>` shell with
**zero `og:` tags** **(verified 2026-08-26, against a real private
post)**; a public post yields the full `og:` set **(verified
2026-08-27)**. The driver's park decision reads the body it landed on —
no redirect inspection needed, which is why the transport seam stays as
it is.

The `og:`-less shell parks `manual` with a reason that names the cause
and the way through — *private or unavailable; screenshot it and share
that if you want it*. Deliberately not a generic fetch failure and
deliberately not `dead`: the content exists, the owner can see it, and
only this transport cannot. Condemning it would be the terminal-mislabel
class the pipeline design exists to prevent.

**An `og:`-less page that is not Instagram's shell is not "private".**
A proxy or CDN error page has no `og:` tags either, and an unmaintained
freebie serves those routinely. The private park therefore requires the
shell fingerprint (`<title>Instagram</title>`, no `og:`); an `og:`-less
body without it classifies `blocked` — the world misbehaving, retried on
the normal lifecycle. Connection failures and 4xx/5xx were already
`blocked`/classified through the standard path.

The media stage gets one guard of its own: a **zero-byte 2xx body is
`blocked`, not a landed file**. The proxy answers out-of-range — and,
plausibly, a transient bad day — with exactly that shape **(verified
2026-08-26)**, and today's media stage would write the empty file and
ledger it `done`. The guard is the media-stage sibling of the enclosure
download's `_not_audio` check, and no real media file is zero bytes.

**Why the private gap is acceptable.** Shareable content comes from
accounts that want to be found, and being private cuts directly against
that. Businesses, influencers and creators publish publicly by design;
private accounts are personal. The gap is real but sits almost entirely
outside the content anyone would capture into a knowledge base.

**The manual fallback needs no machinery.** Screenshot the post and share
the image: binary capture already stages it under `media/<id>/`, and the
cognitive OCR floor already reads it at ingest.

## 7. Config

- `instagram_base_url` — the embed proxy host, for media endpoints.
  Defaults to `https://uuinstagram.com`; overridable per instance, and
  the escape hatch when a host dies or the owner self-hosts.

Nothing else. No credentials, no provider list. One wiring consequence:
`build_drivers` takes no `Config` today, so it grows the parameter (or
the one value) — with a constructor default on the driver so
`default_drivers()` stays config-free for normalize's offline stamping.

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
- **Instagram's embed route** (`/p/<code>/embed/captioned/`) as a
  metadata source — a JS shell with no caption in the served HTML
  **(verified 2026-08-27)**.
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

Ordered; each step is independently reviewable, and the PR stack follows
it.

1. **The bot transport** — the second exported transport in
   `drivers/transport.py` (same implementation, bot UA), with the reason
   inline: it reads as a mistake otherwise. Protocol unchanged.
2. **The media stage's zero-byte guard** (§6) — its own small PR:
   engine-wide, not Instagram-specific, and testable alone.
3. **`Kind.INSTAGRAM`** in `pipeline/types.py`, plus the kind vocabulary
   line in `instance/skills/dex-run/references/schema.md`, same commit.
   No migration — a new kind moves no existing state.
4. **`drivers/instagram.py`** — `matches` (whole-host, §2), `canonical`,
   `fetch` against `SourceDriver`. The instagram.com page fetch and its
   `og:` parse (newline-spanning attribute values, HTML entities
   decoded); the shell-fingerprint park split (§6); the long-token
   resolve off `rel="canonical"`; non-post path shapes parked with their
   evidence. HTTP through the transport seam, bot transport as the
   injected default.
5. **The probe walk** (§3) — indexes 1..5 against `/videos/<code>/<n>`
   with `limit=0`, typed by content type, terminated by the zero-byte
   200, paced 1s. Image indexes emit as proxy `/images/` URLs, screened
   by the media stage's existing verbatim-URL checks.
6. **Registry and config** — `instagram_base_url` on `Config` with its
   row in `state-formats.md`; `build_drivers` wiring (§7); insert ahead
   of `WebDriver`, which stays last.
7. **Transcription wiring** — the video outcome as
   `NeedsCapability(need=Need.TRANSCRIBE)`, caption as `body`, video URL
   under the `enclosure` meta key (§5). `Kind.INSTAGRAM` into all
   **three** run-layer sites together: `_acquire_audio` (modelled on the
   podcast enclosure download), the transcript-body dispatch, and the
   dead-enclosure→manual arm of `_apply_acquisition_failure`. The
   no-speech park reason per §5. The one step that leaves `drivers/`,
   and the one most likely to need its own review.
8. **Tests** — canonicalization across all five path shapes and both
   shortcode grammars; the bot-UA page fetches; the og parse including a
   multi-line caption and the empty-caption case; the probe walk over
   image, video, and truncated-carousel fixtures including the lying
   `og:video` page; the shell-fingerprint split (private park vs proxy
   garbage → blocked); the zero-byte media guard. Fixtures captured from
   the real responses recorded in this document.
9. **Two live checks**, both `ci_hostile` — instagram.com's bot-UA `og:`
   service and the proxy's probe shapes are each somebody else's
   behaviour toward an anonymous fetcher, exactly the class a datacenter
   IP gets answered differently on, and a daily failure mail on someone
   else's uptime is how a real alarm becomes noise.
10. **README** — extend the `dex-enrich run` row, which enumerates what
    the drivers fetch.

## 10. Open, deliberately

- Whether a share-token page on instagram.com carries the
  `rel="canonical"` short code the resolve step reads (§2). Cheap to
  settle with one real share of a restricted-then-public post; until
  then a public long-token share that resolves nothing parks as §6.
- Whether instagram.com's bot-UA `og:` service holds for a datacenter IP
  and under repetition — the live check exists to answer it.
- Whether a genuinely mixed video+image carousel occurs through the
  probe walk (§5 covers it by rule either way).
