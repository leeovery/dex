# Driver-based ingestion pipeline

Status: **agreed design, pre-implementation** (discussed and signed off
2026-08-19/20). This document is the reference for the rewrite of the
engine's ingestion machinery. The roadmap items it resolves point here.

This document was verified line-by-line against the full design-session
transcript, which is held locally by the owner and is not part of the repo;
consult it if a gap or ambiguity is found here.

Origin: this rewrite is the roadmap's long-standing **driver-based ingestion
architecture** item — "this is the core value of the system — solve it
properly" — queued well before any incident. One incident the day before
the design discussion usefully illustrated a failure class the design
addresses: during a production instance install (2026-08-19), Cloudflare
transiently challenged the enricher's fetch of the owner's own website; the
enricher — unable to distinguish a 403 challenge from a dead domain —
ledgered it `dead` (terminal, never retried), and Claude's in-session heal
(hand-fetching 11 pages) left the ledger contradicting the corpus.

What the design does about that class — honestly stated, since no design
can force a site to serve us: (1) the redesigned web driver often avoids
the block outright (urllib + browser UA; trafilatura demoted to extraction
only — its own fetch client was what got challenged); (2) when blocked,
visible status codes classify it `blocked`, never `dead` — the truth is
recorded; (3) `blocked` retries each run, so transient challenges (this one
was — the same fetch succeeded a day later) heal with zero intervention;
(4) persistent blocks escalate to `manual` on the report, where the
in-session heal remains available — and now ends with `enrich mark`, so
state stays consistent; (5) the floor: nothing is ever silently mislabeled
into a terminal state. "Silently wrong, permanently" becomes "usually
succeeds; otherwise correctly diagnosed, automatically retried, escalated
with state kept honest."

---

## 1. Shape

The ledger is the pipeline's work queue, not a receipt log. Every unit of
work is a ledger entry with a status; runs drain non-terminal entries;
anything discovered mid-flight re-enters the top as a new entry with
provenance.

```
 capture (.md in inbox/)
    │  normalize (offline, fast — kinds stamped pattern-only; provisional
    │  FOREVER: frontmatter kinds are never reconciled, the ledger is
    │  authoritative)
    ▼
 corpus item
    │  urls / files
    ▼
┌────────────────── WORK QUEUE (state/enrichment-ledger.jsonl) ──────────────┐
│  entry = {hash, url, kind, format?, status, needs?, provenance…}           │
└──────┬──────────────────────────────────────────────────────────────────────┘
       │ drain non-terminal entries
       ▼
  ┌─────────┐  url pattern match; if inconclusive, one HEAD request;
  │ DETECT  │  local files: byte-signature sniff (anydoc). Authoritative.
  └────┬────┘
       │ kind (+ format) assigned → driver resolved from ordered registry
       ▼
  ┌─────────┐  driver.canonical(url), driver.sleep,
  │ FETCH   │  driver.fetch(unit) → Result
  └────┬────┘
       │
       ├─▶ body + meta ──▶ WRITE   enrichment/<id>/<kind>-<hash6>.md
       │                           (deterministic name → reruns overwrite,
       │                            never duplicate)
       ├─▶ media urls ──▶ MEDIA    shared stage — see §7
       │
       ├─▶ children ─────────────────────────────┐
       │   (harvest promotions, corrected kind)  │  re-enter queue with
       │                                         ▼  provenance + caps
       │                                 back to WORK QUEUE
       │
       └─▶ needs ──▶ WAITING       status: waiting + needs: <capability>
                                   drained when a provider is available (§6)
```

Caps on re-entry (mechanical backstops, not targets): **max depth 4** (the
shared URL is depth 0), **max 12 fetched URLs per item**. A fired cap is
recorded in the ledger and internal logs — it is a judgment-drift signal for
the health check, never shown on user-facing surfaces.

Children and reruns are ledger entries from birth (`status: queued`). The
drain picks up `queued`, `blocked` (attempts < 5), `waiting` (provider now
available), and `error` (engine now newer).

**Runs complete end-to-end within a session.** Every pipeline run is invoked
from inside a Claude session (ingest, health check, scheduled desktop tasks —
which are real local sessions); there is no headless daemon. The run report
names every item with new or changed content — fresh captures, drained
reruns, newly-drained waiting cohorts — and **the same session immediately
completes the cognitive steps** (harvest → digest → wiki) for all of them,
exactly as for a fresh capture. The enrichment-newer-than-digest comparison
in `enrich status` is purely an **interrupted-session backstop** (a session
died between fetch and digest), never the handoff mechanism.

**Session-end invariant**: everything processable is processed — items are
digested whole, after their children land. The only entries that survive a
session are `waiting` / `blocked` / `error` / `manual`, each parked for a
stated reason, each printed in the report.

**Mid-fetch kind discovery** (a server lied to HEAD; "this is actually a
PDF") — implemented on owner order after an initial unauthorized deferral,
and NOT as a child (early drafts said "child-re-entry"; a child is a
different URL — this is the same URL, same hash, changing its mind): the
driver sniffs the fetched body (magic bytes first, via the central
detection module — bytes decide, never content-type alone) and returns
`Result(status: queued, redetect: Redetection(kind, format))`, identity
only, the one sanctioned queued-from-a-driver shape. The run layer appends
a **superseding line for the same hash** — corrected kind, `via: "sniff"`
— last-per-hash relabels the unit in place and requeues it in-run. Any
prior kind's output file is unlinked **when the corrected unit lands one of
its own**, never at correction time: a corrected fetch that parks must
leave the item exactly as enriched as it found it (unlinking first stripped
an item back to `raw` with its digest orphaned and nothing left to
re-derive from). The ledger keeps the history either way. **A hand-written
output closed with `enrich mark <url> done --path …` counts as one of the
unit's own** and drops the superseded file exactly as a drained one does —
that is the prescribed route out of a correction whose corrected fetch
parks (a scanned PDF, no extractor), and without it the stale
pre-correction view of the item was served to the digest and query layers
permanently. **The file dropped is named, never pattern-matched**: a
candidate is `<kind>-<hash6>.md` for one of the closed set of kinds, and it
must record this unit's `url:` to be dropped at all. `hash6` is six hex
digits, so two units under one item do collide (a real `web-6968e3.md` /
`file-6968e3.md` pair was found by hand), and a name-pattern unlink deleted
the neighbour's enrichment while its ledger line still read `done`. Nothing
is dropped for a replacement that is not itself on disk. Works in both
directions (web→file,
file→web). Loop guard is per-run state: one correction per hash per run,
then `manual` "re-detection loop"; across runs a unit may redetect again —
the world changes.

The run report also derives a listing for **no-source items** (text-only
and image-only captures — no URLs, no work units): they surface as
cognitive work ("awaiting description + digest") so a capture with nothing
to fetch is never invisible to the session.

## 2. Interfaces

`typing.Protocol` throughout (structural interfaces; engine floor is 3.11).
Two families, because two different things are swappable:

```python
class SourceDriver(Protocol):          # one per source shape
    kind: Kind
    sleep: float                       # per-driver politeness delay
    def matches(self, url: str) -> bool: ...
    def canonical(self, url: str) -> str: ...
    def fetch(self, unit: WorkUnit) -> Result: ...

@dataclass
class Result:
    status: Status
    meta: dict                         # passthrough → enrichment frontmatter
    body: str | None
    media: list[str]                   # URLs for the media stage
    children: list[Child]              # (url, via) — re-enter the queue
    needs: Need | None                 # capability job
    reason: str | None                 # mirrors the ledger reason contract:
                                       # required when a driver returns
                                       # manual/skipped (the driver knows
                                       # why), optional waiting/blocked/dead,
                                       # forbidden otherwise. The run layer
                                       # may append classifier context but
                                       # never invents what the driver knew.
    assets: list[Asset]                # extraction assets (bytes) — drivers
                                       # never touch disk; the run layer
                                       # writes them under the §7 caps,
                                       # ledgered via: extract-asset.
                                       # Success-only, validated.

@dataclass
class WorkUnit:                        # what the pipeline hands a driver
    hash: str                          # sha1(work key)[:10]
    url: str                           # canonical URL, or file:<repo-path>
    kind: Kind
    format: Format | None              # file work only
    item: str                          # owning corpus item id
    depth: int                         # 0 = the shared URL
    parent: str | None                 # spawning work unit's hash

Child = (url, via)                     # the PIPELINE assigns depth = parent
                                       # depth + 1, parent = spawner's hash,
                                       # item inherited — drivers never do

@dataclass(frozen=True)
class Availability:                    # never tuple[bool, str] — weak-type trap
    ok: bool
    reason: str = ""

class Transcriber(Protocol):           # a CAPABILITY, not a source
    name: str
    model: str                         # what gets stamped into the §6
                                       # via/model frontmatter — the
                                       # stamping is unimplementable
                                       # without it
    def available(self) -> Availability: ...
    def transcribe(self, audio: Path, initial_prompt: str) -> str: ...

class Extractor(Protocol):
    name: str
    def supports(self, fmt: Format) -> bool: ...
    def available(self) -> Availability: ...
    def extract(self, data: bytes, fmt: Format) -> Extraction: ...

@dataclass(frozen=True)
class Extraction:
    markdown: str
    assets: list[Asset]                # Asset = (data: bytes, suggested_ext)
```

The driver registry is an **explicit ordered list** — `web` last as the
catch-all. No auto-discovery; ordering is semantics. `normalize` imports the
same registry/detect module (kills the historical `kind_of` duplication and
its m.youtube.com divergence).

Typing discipline (binding for the implementation):
- All §2 types are `@dataclass(frozen=True, slots=True, kw_only=True)`.
  `Result` gets defaults (`media`/`children` via `field(default_factory=
  list)`, `needs`/`body` None) so a simple driver returns
  `Result(status=Status.DONE, meta=…, body=…)`. `meta` is
  `dict[str, str | int | None]` (it becomes YAML frontmatter), never bare
  `dict`.
- **WorkUnit is not LedgerEntry.** Drivers see url/kind/format/item/depth —
  never `attempts`, `engine`, `rerun` bookkeeping (no internal types across
  layers).
- Drivers may return only `{done, dead, skipped, manual, blocked, waiting}`
  — `queued` is a birth state and `error` is raised, never returned
  (`Result` has no error channel by design; the pipeline's single broad
  except is the only route to `error` status). `Result` validates this.
- The typed registry literal (`DRIVERS: list[SourceDriver] = […]`) is the
  Protocol-conformance point — the type checker verifies every driver
  against the interface at that one assignment. Same for provider lists.
- Every dispatch over `Status`/`Kind`/`Need` uses `match` +
  `assert_never` (exhaustiveness: adding an enum member becomes a type
  error at every unhandled site). The drain predicate is one total
  function, `is_drainable(entry, ctx) -> bool`, exhaustively matched — the
  highest-value function in the system to make total.

Capability resolution: **per format / per need, first available *mechanical*
provider wins**, order set by instance config. The **cognitive provider** is
always last in resolution order and is the always-available floor for
judgment-shaped work — but `enrich run` never "drains" it: cognitive jobs
are listed on the run report, the session does them with eyes, and appends
the `done` entry. Precisely: **`waiting` means no mechanical provider**; a
job that resolves to the cognitive provider is `waiting` from the queue's
point of view and work-to-do from the session's. No instance ever hard-fails
on a missing key or an unsupported format.

## 3. Enums

`StrEnum` (serializes as plain strings — ledgers and frontmatter stay
human-readable). **No aliases**: renames are a clean break executed by
migration 1 (§12); pre-rename instances are updated by that migration, not by
compatibility code.

**Kind** — source shape, one driver each; two values are corpus-frontmatter
vocabulary only and never become work units:

| kind | driver | notes |
|---|---|---|
| `youtube` | ✓ | captions; fallback → `needs: transcribe`. Identity is the video id (every watch/short-link/live/shorts/embed shape → `watch?v=<id>`); a playlist page keys on its list id and parks `manual` — picking its videos is judgment. Channel addresses (`/@handle` and its tabs, percent-encoded `/%40handle` included, `/user/…`, `/c/…`, `/channel/…`, and the legacy bare vanity name `/veritasium`), hashtag feeds and search-result pages park `manual` the same way and **before the probe**: yt-dlp answers those URLs by enumerating every video the channel holds, and the transcribe drain would then pull every one of those audio files over a single filename. The vanity form has no marker at all — youtube.com's root namespace belongs to channels — so the driver names youtube.com's own **functional first segments** (`/watch`, `/playlist`, `/results`, `/feed`, `/embed`, `/live`, `/shorts`, `/v`, plus the product and account surfaces) and reads every other bare root segment as a channel. The list errs one way on purpose: a functional segment missing from it parks `manual`, one recoverable ledger line, while a channel mistaken for a functional path reaches the probe and enumerates thousands of videos. A channel's `/live` path is one video and still fetches |
| `x` | ✓ | renamed from `tweet`; thread walk-up (§8) |
| `github` | ✓ | repos / profiles / gists / issues / blobs. Every route goes through the authenticated `gh` CLI, blobs included — `raw.githubusercontent.com` is unauthenticated, so it 404s every private-repo blob however the machine is signed in, and that 404 classified live content `dead`. Losing raw.githubusercontent.com costs the **ref/path boundary**, which that host resolved server-side and the contents API cannot: branch names hold slashes (`blob/automation/bors/auto/README.md`), and the `blob/refs/heads/<branch>/` permalink form spends two segments before the name even begins, so splitting at the first segment sent a wrong `?ref=` with a wrong path and 404'd live files into `dead`. The driver guesses the shortest ref the URL's shape allows — free, and right for nearly every link — and only when that 404s asks `git/matching-refs/<heads|tags>/<first segment>` which of the repo's own refs the path starts with, matching segment-wise (`automation/bors` must not claim a URL whose branch is `automation/bors-next`) and taking the longest. A repo with thousands of branches costs the same one page as a repo with three. When no ref re-splits the URL, or the lookup itself fails, the original classification stands: a genuinely missing path is still `dead`, never unclassifiable. A blob too large for the contents API to serve inline parks `manual`, never `dead`. github.com's **reserved first segments** (`/features`, `/topics`, `/sponsors`, `/orgs`, `/collections`, `/marketplace`, `/trending`, `/about`, `/pricing`, `/settings`, `/explore`, …) can be neither user nor repo, so the driver declines them and registry order hands them to `web`, which extracts them like any page — driving them as repo work 404'd pages that render fine in a browser into `dead`. Only the first segment is screened: `acme/topics` is an ordinary repo. **Blob bytes are sniffed — by filename — before they are fenced**: a PDF or any other binary committed to a repo parks `manual` naming what it is, never a code fence full of replacement characters ledgered `done`. The name is what catches the two extractable shapes that carry no byte signature: a CSV, and an unsmudged **Git-LFS pointer**, whose 130 bytes of `oid sha256:…` decode as clean UTF-8 and fenced `done` as though they were the document they stand for (the contents API serves the pointer, never the object). A committed CSV parks the same way, deliberately — allowing text-shaped documents to fence is exactly what let the LFS pointer for a `.csv` through, and a captured CSV reaches `csv-builtin` as a real table instead of a fence truncated at 40k characters. It does not re-detect to `file` work, because a GitHub blob URL serves an HTML viewer rather than the bytes — the file driver would fetch that page, find HTML, and re-detect straight back (a loop park on a public repo, `dead` on a private one). The rescue route is capturing the file itself, which is already `file` work |
| `paper` | ✓ | arxiv / openreview / hf-papers |
| `podcast` | ✓ | new — Apple/Spotify/RSS episode links (§9) |
| `web` | ✓ | renamed from `blog`; registry catch-all, always last |
| `file` | ✓ | URL-served or captured binaries; routes by Format |
| `image` | — | corpus-only: described cognitively at ingest |
| `text` | — | corpus-only: note-only capture, nothing to fetch |

**Format** — extractor routing only (not images, not audio):
`PDF, DOCX, PPTX, XLSX, ODT, ODS, ODP, RTF, EPUB, CSV`.

**Status** — `queued, done, dead, skipped, manual, waiting, blocked, error`
(§5).

**Need** — `transcribe, extract, ocr`. Needs are mechanical and
resource-keyed only. Cognitive obligations on *items* (re-judge under new
harvest rules, refresh a stale digest) are never queued — they are **derived
state**, computed on demand from files already on disk (`passes.jsonl` rules
version vs the current constant; digest date vs the ledger's last `done` for
the item). Mechanical obligations are queued because they're work to be
done; staleness is derived because it's a fact that shows.

**`via`** (provenance) stays a documented string, not an enum —
`harvest, thread, media, sniff, extract-asset, migration-<n>` — because
`migration-<n>` is parameterized and provenance is descriptive, never
dispatched on. **One blessed exception**: the run loop routes
`via == "media"` entries to the media redrain — §7 mandates media entries
carry the parent's kind, leaving `via` as their only marker. That single
commented dispatch site is the sanctioned total of value-routed dispatch,
forever — and it dispatches on a controlled single-token field, not prose.

**Lifecycle signals are typed schema fields, never prose.** `reason` is
display text: reports render it, nothing matches on it, and rewording one
can never change routing. The two signals that once looked reason-shaped
are §5 fields instead: a blocked audio-acquisition retry carries
`needs: transcribe` (routing it back through the transcribe drain, not the
driver), and a cap-fire marker line carries `capped: true`. This keeps the
`reason` namespace safe for session-authored free text
(`enrich mark --reason …`), which must never be able to collide with a
routing signal.

## 4. State

**JSONL, ratified; SQLite rejected.** State travels through git between
machines (owner's desktop, other owners' laptops, scheduled sessions —
environments lacking dex's declared dependencies, such as the Cowork VM
class, are not dex environments at all, per §13). Git cannot merge a SQLite blob; JSONL merges as a union —
`.gitattributes` (synced) sets `merge=union` on **all `state/*.jsonl` files**
(all four are append-only). Diffability is
load-bearing (audit-by-git-log is free); Claude greps state directly in
sessions. If an instance ever outgrows this: SQLite as a derived, gitignored
cache rebuilt from the JSONL — the ledger stays the source of truth.

Rule: **state Claude reasons over is markdown (digests); state machinery
processes is JSONL/JSON; nothing binary in `state/`.**

Files:

| file | holds |
|---|---|
| `state/enrichment-ledger.jsonl` | work units (§5) |
| `state/passes.jsonl` | per-item stage records — `{stage: harvest, item, rules, date}`; "ran and promoted nothing" must be distinguishable from "never ran" |
| `state/migrations.jsonl` | applied-migrations log (§12) |
| `state/issue-reports.jsonl` | filed/commented issue fingerprints (§13) |
| `state/config.json` | instance config — renamed from `normalize-config.json` (migration); holds `media_fetch`, `transcribe_model`, `transcribe_base_url`/`_api_key`/`_api_model`, `report_issues`, `internal_domains`, `noise_prefixes`, and provider order as `providers: {<capability>: [<name>, …]}`; unknown keys rejected loudly |
| `cache/` (gitignored) | ephemeral: render payloads, in-flight audio. Never state, never synced. |

Ledger mechanics: append-only, full-record lines, **last-per-hash wins**.
`enrich compact` rewrites keeping only the latest line per hash (also settles
union merges). Superseded lines until then are the audit trail.

## 5. Ledger entry schema and status lifecycle

```jsonc
{
  "hash": "73bd784849",        // key: sha1(work key)[:10] — canonical URL,
                               // or "file:<repo-path>" for local files
  "url": "https://…",          // canonical form (or repo path)
  "item": "2026-08-19-…-55ad7b",
  "kind": "web",
  "format": "pdf",             // file work only
  "status": "waiting",
  "needs": "transcribe",       // waiting parks (required) and blocked
                               // acquisition retries — the typed routing
                               // signal back to the capability drain
  "attempts": 3,               // blocked only (error's retry gate is the
                               // engine field — once per newer engine)
  "capped": true,              // skipped only, serialized only when true:
                               // a cap-fire marker — refused work, not an
                               // admitted unit
  "engine": "0.2.1",           // engine version that wrote this line
  "date": "2026-08-19",
  // provenance — children and reruns only
  "via": "harvest",
  "parent": "a1b2c3d4e5",
  "depth": 2,
  "rerun": true,               // requeued over existing output — overwrite
  // outputs — success only
  "path": "enrichment/<id>/web-73bd78.md",
  "title": "…",
  "error": "…",                // scrubbed message — error entries only
  "reason": "…"                // the stated parking reason (§1): REQUIRED on
                               // manual/skipped, optional on
                               // waiting/blocked/dead, forbidden on
                               // done/queued/error (error has its own field).
                               // Display prose only — the engine never
                               // matches on it (§3)
}
```

| status | meaning | retry |
|---|---|---|
| `queued` | exists, not yet attempted (children, reruns, seeds) | next run |
| `done` | output written | never (until a migration requeues) |
| `dead` | confirmed gone — DNS / 404 | never |
| `skipped` | deliberately not fetched | never |
| `manual` | needs a human/Claude decision | surfaced, never mechanical |
| `waiting` | no *mechanical* provider for `needs` (cognitive jobs surface on the report for the session) | when the capability appears — no clock |
| `blocked` | world misbehaved — 403/429/503 | every run; 5 attempts → `manual` |
| `error` | engine bug (files an issue, §13) | **once per engine version** |

`error` retry-on-new-engine: retrying a deterministic bug on the same code is
waste; the entry records the engine version that failed and re-attempts only
when the running engine is newer. This closes the self-healing loop with zero
redundant work: bug files issue → fix → release → sync bumps pin → next run
retries → `done`.

Blocked-vs-dead requires visible status codes: the web driver fetches via
urllib with a browser UA and hands HTML to trafilatura for **extraction
only** (`trafilatura.fetch_url`'s failure mode — `None` for everything — is
what caused the motivating incident). Wayback fallback stays — and its
failures are classified like any fetch, never swallowed.

**Failure classification is centralized, never per-driver** (the motivating
incident was a classification bug in one fetcher; seven drivers classifying
independently is seven chances to reintroduce it):
- One `classify_http(status_code) -> Classification` (+ DNS/connection-
  error mapping) in `pipeline/` — returning **status AND reason together**
  (402 → manual demands a stated reason; per-driver reason invention would
  decentralize exactly what this centralizes). Mapping: 403/429/5xx →
  `blocked`, 404/410/NXDOMAIN → `dead`, **401/402 → `manual`**
  (login-walls/paywalls — x.com answers 402; retrying never resolves
  payment-required), and **any other status the transport surfaces
  (3xx redirect loops included) → `blocked` with "unexpected HTTP <n>"** —
  the classifier is total; no HTTP outcome is unclassifiable. Routed
  through by every driver's HTTP path. The §15 regression pin tests the
  classifier once and holds for all drivers.
- **Media URLs are hashed un-canonicalized** — signed query params ARE the
  resource. Side effect, accepted: an expiring signed URL re-mints a fresh
  entry per parent rerun, and the stale one retires through normal blocked
  → manual escalation.
- **200-but-thin is `manual`, never `dead`** (learned from the 2026-08-20
  overnight runs: JS-rendered SPA pages were ledgered `dead` and their
  items stranded at `raw`): a successful fetch whose extraction comes back
  empty/thin means *our tooling can't read it*, not that it's gone —
  cognitively rescuable, so it parks for judgment with reason
  `thin-extraction`.
- Providers **raise** a typed `ProviderInputError` for bad inputs (corrupt
  audio, unparseable file); the run loop maps it → `manual`, anything
  uncaught → `error`. Classification judgment lives in one place.
- Exactly **one** `except Exception` in the whole pipeline: the per-unit
  loop in `run.py` (→ `error` + issue filing). **Every** dispatch route runs
  inside it — driver fetch, transcribe drain, and media download alike,
  the media stage's own first download included — so no unit's failure can
  escape the drain or be charged to another unit's line.
  Drivers and providers never
  broad-catch; internal raises use `raise … from e` so filed tracebacks
  keep their cause. Migrations have the one counterpart outside the
  pipeline, on the same reasoning: canonicalizing one stored URL runs a
  driver's arbitrary code over owner data, and a migration that dies
  part-way leaves state half-moved — so the per-entry canonicalization is
  broad-caught and skipped-with-why (§12). The scrubber feeds the ledger `error` field; issue
  bodies carry no free text at all (the filer ruling in the issue-filer
  section).

Engine-version comparisons (error retry-on-new-engine, sync) parse to
tuples — `"0.10.0" > "0.9.1"` is False as strings, and that bug would file
issues about itself.

**LedgerEntry is a frozen, validated dataclass, not a dict**: the schema
comments above are runtime invariants enforced in `__post_init__`
(`waiting` ⇒ `needs` present; `needs` on `waiting`/`blocked` only;
`blocked` ⇒ `attempts ≥ 1`; `capped` on `skipped` only; `error` ⇒
`error` + `engine` present; `done` with output ⇒ `path` present; `via`
validated against the known prefixes). Serialization happens in exactly one
place (`ledger.py`: `from_line`/`to_line`, dropping None fields); a
nonconforming line raises a `LedgerSchemaError` naming the likely missing
migration instead of a `KeyError` three stages later. `MigrationReport`
gets the same dataclass treatment (§12) since surfaces render it.

Recorded edge: a URL captured into two items dedupes by hash and enriches
under whichever item hit first; the second item's frontmatter won't list it.
Same as today, now documented.

**Dead-link policy**: no proactive link scanning, ever. URL death is
recorded opportunistically when a fetch encounters it; content is never
removed because its source died — content outliving links is the system's
whole bet (it's why enrichment and the wayback fallback exist). Deliberate
removal is the separate source-removal roadmap item.

**Parked items still exist**: when enrichment parks (`waiting` on an
unsupported format, `blocked`, `manual`), the corpus item was already
created at ingest — provenance and the owner's note are captured
immediately and never queued. The item stays `status: raw` with no
digest/wiki work until its work units complete.

Heals write the ledger: when Claude repairs something by hand (the
Cloudflare-403 incident above), the ingest skill's heal procedure ends by
appending a corrected ledger entry — last-per-hash makes this the
mechanism, not a hack.

## 6. Capabilities

**transcribe**
- `whisper-local` — default, free floor. `faster-whisper` (MIT), CPU int8,
  fine on Apple Silicon; no GPU assumptions. Default model `medium`
  (config: `transcribe_model`; per-call `--model` override — skill guidance:
  drop to `small` for long/backlogged queues, stay up for dense technical
  audio). First run downloads the model (~HF cache, once per machine) — the
  report surfaces it so slow-first-run is explained. No file-length limits.
- `whisper-api` — one provider class, OpenAI-compatible, `base_url` + key
  from config/env. Pointed at Groq et al: GPU-fast, ~pennies/hour (roadmap
  item tracks the provider investigation). **ffmpeg chunking** (~20-min
  segments, transcripts concatenated) removes upload limits for any provider,
  including OpenAI's 25MB cap.
- **Audio acquisition belongs to the drain, not the drivers**: the
  transcribe drain obtains audio itself — yt-dlp download for YouTube
  entries, HTTP GET of the enclosure for podcasts — into `cache/audio/`,
  transcribes, deletes on success (§9 lifecycle). Drivers only ever emit
  `needs: transcribe` with the pointer.
- Accuracy: **`initial_prompt` priming** with the item's known vocabulary
  (video title + description, episode title + show notes) — mechanical, free,
  large win on names/jargon. Transcripts are stored **raw**, stamped
  `via`/`model` in frontmatter; corrections live downstream in digest/wiki
  where judgment already operates — enrichment stays the mechanical record.

**extract**
- `anydoc` (firecrawl-anydoc, MIT, PyPI) — default, all ten formats.
  Scanned/image-only pages → `needs: ocr`. Embedded assets come back as
  **bytes** — structurally, not as an anydoc quirk: embedded images live
  inside the container and have no URL, so `Extraction.assets = bytes` is
  part of the contract; the extract step writes them directly to
  `enrichment/<id>/` under the media caps, ledgered `via: extract-asset`. A
  replacement extractor either populates assets (bytes, the only possible
  form) or returns none — graceful text-only degradation. Images merely
  *linked* from a document stay links in the markdown, like web body links —
  harvest judgment promotes them if they matter.
- `csv-builtin` — stdlib, zero deps.
- `cognitive` — floor: parks `needs: extract` for the ingest session.
- The **Format is the contract, not the tool**: providers register per
  format; if anydoc dies, each format falls back independently (or parks) and
  the system's shape is unchanged.

**ocr**
- `cognitive` only, for now (Claude vision at ingest). Engine providers may
  come later; the interface is already there.

Provider contract: `available()` failures (model missing, broken install)
park jobs as `waiting` with the reason — the wait list is normally empty for
transcription, not absent. A provider raises `ProviderInputError` for
bad-input cases (corrupt audio, malformed file) → the run layer maps it
`manual`; **`ProviderUnavailableError`** for call-time availability
failures (API 5xx/429, missing binary, model-download failure) → the job
**re-parks `waiting`** with the reason, never burning blocked attempts; an
uncaught crash is an engine bug and takes the `error` path (issue filed,
retry on new engine). The availability seam is **per-format** —
`available(need, format)` — so a PDF wait never wakes for a CSV-only
provider. **Acquisition failures are not provider failures**: a failed
audio download (yt-dlp breakage, blocked enclosure GET) classifies through
the normal §5 lifecycle — `blocked` with attempts, escalating to `manual`
at 5 — never as no-clock waiting. The blocked line keeps
`needs: transcribe`, so the retry routes back through the transcribe drain
rather than the driver (§3: a typed field, never the reason's wording).
Config keys: `transcribe_base_url`,
`transcribe_api_key`, `transcribe_api_model` (the API-side model name;
local size names stay in `transcribe_model`). **The API key's home is the
instance's gitignored `.env`** (`OPENAI_API_KEY=…`; `OPENAI_BASE_URL` works
too) — the enrich CLI folds `.env` into the environment before building
capabilities, real environment variables winning, so secrets never sit in
the committed config; the config keys carry the non-secrets
(base_url/model) and exist for the rare deliberate committed-key case. A
waiting-transcribe park
that carries an enclosure pointer **always writes its park file** — §9's
round-trip depends on the frontmatter pointer existing, show notes or not.

**Capability report** (a render surface): each capability, active provider,
dormant upgrades and what they'd need —
`transcribe: whisper-local (active) · whisper-api available — set OPENAI_API_KEY`.
Discoverable, never nagging. This is how a free-floor instance learns what a
key would buy.

## 7. Media stage

A shared pipeline stage, **not** a capability (no plausible second
implementation of an HTTP GET). **URL downloads only** — extraction's
embedded assets never pass through here (§6). Drivers return media URLs in
`Result.media`;
the stage downloads to `enrichment/<id>/media-N.ext`, honoring instance
config (`media_fetch: none | lead`), **cap 4 files, ~10MB per-file ceiling**;
every download ledgered (`via: media`, parent = owning work unit, kind =
parent's kind) — success `done`, transient failure `blocked` (normal retry
rules), oversize `skipped` with reason. Media downloads do **not** count
toward the item's 12-URL cap (that cap bounds fetched pages). The old
silent `except: pass` dies here.

**Media URLs are validated before they are ledgered.** The stage fetches
what a driver hands it verbatim, so a value that could never be a request —
a non-http(s) scheme, no host, whitespace or control characters (a
page-relative `og:image` was the wild case) — parks `manual` as its own
media unit with the refusal stated, and never enters the queue. Drivers
absolutize and screen the URLs they emit; the stage assumes nothing. A
driver that cannot read a media URL **whole** emits nothing for it rather
than a repaired guess: a line-wrapped `og:image` content attribute yields
no media at all, because the truncated head of it (`https://cdn.example.test/`)
is a well-formed request for a resource that does not exist — a guaranteed
junk fetch ledgered as a real media unit.
Downloads run through the same per-unit protection as every other unit
(§5): a media failure is charged to the media unit, never to the page whose
markup named it. **The `N` in `media-N.ext` is the unit's position among the
item's media units in ledger order**, not the next free index on disk — a
crash between the file write and the outcome line is overwritten by the
redrain, never duplicated beside. **`N` names the file; it never decides
the cap.** The cap counts media-family files that exist, so a unit's
parked, dead or skipped siblings spend none of it and an `N` past the cap
is ordinary: a thread pooling six photos whose first two 404 still lands
four files, at `media-2` through `media-5`. Counting positions instead
recorded a terminal `skipped — media cap (4 files) reached` on units no
file had displaced, losing them permanently under a reason that was false.
**`media-N.md` is not a media file**: it is the session's written
description of a media capture, counted by neither the cap nor
the "this slot already holds my own file" check — reading it as either put
a download beyond the cap and beside a description of something else.

## 8. X driver (renamed from tweet)

- **A post's identity is its status id.** Every share shape — the username
  form, the X app's `/i/web/status/<id>`, `/i/status/<id>`, the legacy
  `/statuses/` spelling, `/photo/1`-style tails, share params, and the
  twitter.com / mobile.twitter.com hosts — canonicalizes to
  `https://x.com/i/status/<id>`, so one post is one work unit however it
  was shared. Fetches address fxtwitter by the bare `status/<id>` path,
  the one shape it serves for all of them: the API ignores a username
  segment but 404s on `/i/web/…`, and that 404 would misread as a dead
  post — the terminal-mislabel class this design exists to kill.
- **Thread walk-up inside the driver**, not via children: the chain is
  context for the captured post, not new first-class sources. One enrichment
  file, one ledger entry. fxtwitter parent pointers, **cap 20 hops**, all
  authors included, cap-hit noted in the ledger only.
- Fetch order is bottom-to-top (parent pointers); **storage is reading
  order** — root first, captured post last, each post attributed
  (`@who — date`); frontmatter records which post was captured. A
  thread-as-one-long-post reads as written. Capture technique: sharing a
  thread's *last* post rolls up the whole thread.
- Quoted posts stay inline (blockquote); promoting a quote is a harvest
  judgment, not driver mechanics.
- **Long-form articles render from `article`, not `text`.** fxtwitter
  returns `text: ""` for them, the prose under `article.title` /
  `article.preview_text`, and `raw_text.text` holding only the shortlink to
  the article; the body is title + preview text + that link. A body that is
  nothing but a shortlink is **not content** — the "no text or media"
  `manual` park applies, because a post ledgered `done` on ~74 characters of
  URL is thin garbage the digest layer cannot tell from real capture.
- Chain media pooled, captured post's first, media-stage cap applies.
- **Incomplete chains are recorded, never silently presented as complete**
  (learned from a 2026-08-20 production run — a thread's root promised
  "8 things", six existed publicly): a parent fetch failing mid-walk is
  mechanical — the driver records the gap in meta
  (`chain_incomplete: true` + how far it got). A chain that's
  *semantically* short (deleted/restricted posts detectable only by
  reading) is cognitive — a digest note, per current practice.
- **The recording site for walk-up caps and gaps is enrichment
  frontmatter** (`thread_cap_hit`, `chain_incomplete`), not the ledger —
  amended at phase-2 review: the §5 schema forbids `reason` on `done`
  entries and drivers never write the ledger, so §8's earlier
  "ledger-noted" wording was unimplementable as written. The health
  check's judgment-drift scan greps enrichment frontmatter for these
  markers accordingly.
- **Walk-down is explicitly unsolved** (no clean API; no scraper
  dependency). Backlog.
- Engagement counts stay unrecorded (snapshot noise), per the standing rule.

## 9. Podcast driver (new)

Today a Spotify/Apple link captures marketing chrome. Podcasting is RSS
underneath; the audio lives in the feed's `<enclosure>`:

- **Apple link** → iTunes lookup API (public, keyless) on the **show** id
  from the `/idNNNN` path segment → match the episode by `trackId` against
  the `?i=` value inside the returned window (200 episodes, the largest the
  API serves) → show RSS → enclosure. The lookup API resolves show ids
  only: handed an episode id it answers `resultCount: 0` for every episode
  that exists. An episode older than the window parks `manual` saying so.
- **Spotify link** → og-title from the page → iTunes *search* → RSS → match.
  An enclosure that 404s at drain time is treated as an expired signed URL
  → `manual` with a re-resolve route, never `dead` — expired links are not
  gone episodes (blessed at phase-3 review, overriding the plain 404
  mapping for this one case).
  Spotify exclusives fail honestly → `manual` (Claude may rescue via the
  show's own site).
- **Direct RSS / indie episode page** → enclosure or `<link rel>` in head.

Then: audio → `cache/audio/<hash>` → `needs: transcribe` → whisper drains
(primed with title + show notes). Show notes from the **feed** (richer than
the page) written into the enrichment; their links are harvestable like any
content. Audio lifecycle: stays in cache while pending/blocked (retries don't
re-download 150MB), **deleted on successful transcription** — the audio is a
digestion mechanism, not what was shared; the transcript supersedes it; the
enclosure URL in frontmatter is the re-fetch pointer.

## 10. Harvest: the subject rule (replaces one-hop)

At each fetched page, Claude may promote links that are **primary artifacts
of the item's subject** (the project's repo, homepage, docs, the paper);
links that leave the subject (similar-projects, blogrolls, footers) are never
promoted, at any depth. Judgment is cognitive (harvest is already Claude's
step); the engine bounds it mechanically: promoted children re-enter with
`{parent, via: harvest, depth}`, caps depth 4 / 12 URLs per item.

Engine primitive: `dex enrich fetch <item-id> <url>…` — fetch specific extra
URLs into an existing item, ledgered as child entries. Semantics: `parent`
defaults to the item's primary work unit (override with `--parent <hash>`),
`depth` = parent's depth + 1, and fetches count against the item's 12-URL
cap; `--force` may exceed the cap for an owner-requested deepen (the cap
fire is still recorded). **One bad URL never aborts the batch**: an
uncanonicalizable URL parks as a bad seed, a URL already enriching under
another item is reported (one URL enriches under one item — the batch
continues without it), and a capped refusal is reported with the `--force`
route it names — the owner asked, so every refusal comes back as an answer
on the report, and the rest of the batch still fetches. This is also how
Claude deepens any item on
request, and it absorbs the former "site driver" idea: a thin landing page
whose substance is on /pricing and /docs is the subject rule applied to the
site's own pages.

**Promoted URLs live in the ledger only** (`parent`, `via: harvest`, `item`).
The corpus item's `urls:` frontmatter is capture provenance — what was
shared — and is immutable after ingest. "What was fetched for this item" is
a ledger query (`enrich status --item <id>` surface); the enrichment
directory listing already shows the outputs.

Prerequisite fix: the web driver **keeps hyperlinks** in extracted markdown
(the old `include_links=False` stripped the very URLs harvest reasons over).
Harvest-rule changes bump a version constant in the engine; passes are
recorded in `state/passes.jsonl`; re-assessment of old items is
migration-seeded (§12), not scan-inferred.

## 11. Rendering: judgment decides, code renders

Ported from the agentic-workflows kernel/surfaces architecture. Layout that
is fully determined by data is computed in code and emitted verbatim — never
re-derived character-by-character by the model.

```
render/kernel.py     pure layout, zero dex vocabulary — wrap/width/column
                     math, tables, trees, kv blocks. Alignment bugs can
                     exist in exactly one place, and it has tests.
render/surfaces.py   named surfaces, loud payload validation:
                     enrich-report, status, capability-report,
                     ingest-receipt, health-report, sync-report
```

Two call paths: engine-internal (e.g. `enrich run` renders its own report
in-process — includes a "reported upstream: N issues" line when the filer
acted) and cognitive (Claude writes a JSON payload — including free-prose
fields like `judgment_notes` — to `cache/`, runs `bin/dex render --file …`,
emits the result verbatim; even prose position/framing is deterministic).
Standing skill rule: **never hand-draw a table or report; there is a surface
for it — call it.** Cap-fired events are internal (ledger/log) and appear on
no user-facing surface.

## 12. Releases, sync, migrations

**Tag-pinned, auto-upgrading.** The `bin/dex` shim reads a pinned tag from
**`.dex-engine-pin`** (one line, instance root, committed, instance-owned —
sync writes its *value*, it is not a synced-template file) and runs
`uvx --from git+…@<tag>`. Bootstrap: the first tag-aware sync creates the
file pinned to that sync's own release. Mint
cuts releases and changelogs — human-invoked, never the engine; the engine
only consumes releases. A broken push to main reaches nobody until tagged;
rollback is editing the pin line.

Sync flow (**step 0 of every dex-run session** — scheduled, "process now",
or health check — per the ratified operating walkthrough; also on demand):

```
1. read pin → check latest release (git ls-remote)
2. newer release? → bump pin, RE-EXEC sync at the new tag
   (majors auto-apply too, announcing loudly)
3. run pending migrations   (new code, before anything touches state)
4. sync skills from the same tag   (code/skills/state aligned by construction:
   everything ships in one tag; sync copies from the bundled template of the
   running — pinned — version)
5. render the sync report (one surface)
```

Majors also auto-apply (the always-migratable commitment) but announce loudly
in session and health report. Minors only for the foreseeable future.
Transition bootstrap is automatic: sync rewrites its own shim, so existing
instances move from main-tracking to tag-pinning on their next sync.

**Migrations** — `src/dex_engine/migrations/migration_<n>.py`, plain
integers, runner sorts numerically (padding buys nothing; single author +
release-serialized numbering means no collisions). Applied log
`state/migrations.jsonl`, one record per applied migration:
`{number, engine, date}` (union-merge; idempotency is the backstop when
machines race an un-pulled repo). Run by sync before anything else.

```python
class Migration(Protocol):
    number: int
    intent: str                        # what this is MEANT to do
    def apply(self, root: Path) -> Report
        # Report = {actions, skipped: [{what, why}], anomalies}
```

Authoring rules:
- **Mechanical and near-instant only.** Migrations never fetch, never
  transform content, never re-implement pipeline stages.
- Exactly two permitted acts: **provably-safe state rewrites** (renames,
  key changes) and **queue seeding with provenance**
  (`{status: queued, rerun: true, via: migration-<n>}`; a status
  translation may instead seed `waiting` + `needs`, as migration 1 does).
  The pipeline does the real work with current code, through the front
  door.
- Reruns overwrite deterministically-named outputs; "changed" is a byte
  compare against the prior file before overwrite, and the run report lists
  changed items. Those are completed cognitively **in the same session**,
  from the report (§1); the enrichment-newer-than-digest derivation catches
  interrupted sessions. Nothing rerun-related is ever queued as a cognitive
  need.
- The Report is rendered via a surface and **reviewed by Claude in-session**
  (a session is always present in this system): `skipped` and `anomalies`
  are repaired with judgment — code declines what it can't do safely
  (hand-healed files with nonconforming names, frontmatter that doesn't
  parse) and says so, rather than guessing.
- **Untranslatable ledger lines are QUARANTINED, never left in place**
  (amended at phase-4 review): a line a migration cannot provably
  translate moves verbatim to `state/enrichment-ledger.unmigrated.jsonl`,
  named in the report with the concrete repair procedure: cross-check each
  line against `state/exclusions.tsv` FIRST — a line referencing an
  excluded item is a confirmed loss, closed out and never re-added; only
  then review the remainder (re-add via
  `enrich mark <url> <status> --reason …`, or accept the loss). The
  main ledger must load clean after every migration — a skipped line that
  poisons `ledger.load` also bricks `enrich mark`, the sanctioned repair
  verb, leaving judgment with no working tool. Lint flags a non-empty
  quarantine file at every health check.

Shipping migrations for this rewrite:
1. **Renames + status vocabulary** — `tweet→x`, `blog→web` in corpus
   `kinds:`, enrichment filenames, ledger; `normalize-config.json →
   config.json` — dropping `name_map` on the way (owner ruling: never
   applied by any engine version, source-specific to the Discord/Space
   backfill; and the new Config rejects unknown keys loudly, so carrying
   it forward would brick every command). Old ledger statuses translate: `nocaptions → waiting,
   needs: transcribe` and `toolong → waiting, needs: transcribe` — both
   **immediately drainable** now (whisper-local exists; chunking removed the
   length limit), resurrecting work the old system permanently gave up on.
   Old `error` entries adopt retry-on-new-engine semantics. The old ledger
   used `error` as an informal reason field on non-error statuses (28 wild
   lines: dead/nocaptions/done/skipped/manual, plus one `note` field) —
   migration 1 **ports those values into `reason`**, never destroys them.
   Must precede any requeue (else reruns write `x-….md` beside stale
   `tweet-….md`).
2. **Identity re-key + rerun seed** — two steps over one ledger pass, in
   this order. First, identities: the rewrite moved canonical identity
   for two kinds (x posts are keyed by status id; youtube's single-video
   shapes collapse to `watch?v=<id>`), so **every** ledger entry is
   re-keyed to the identity the current driver registry computes —
   recompute `work_hash(canonical_url(url))` and, where it differs from
   the stored hash, rewrite the line in place under the new hash +
   canonical URL with every other field preserved. Statuses are sacred: a
   `manual` verdict stays `manual` under its new identity, a `done` stays
   `done` — without this pass, the first run's corpus seeding would mint
   fresh queued units under the new hashes and re-fetch work already done
   or deliberately parked. Where two old spellings collapse to one new
   hash (the x.com and twitter.com forms of one post), file order is kept
   and the ledger's own last-per-hash rule decides — the later line wins;
   the report names the collapse. A moved hash takes its pointers with it:
   a child's `parent` naming a re-keyed hash is rewritten in the same pass
   (the file is read twice, written once — a parent's line may come after
   its child's). Entries whose key is **verbatim by design** are left
   alone, because re-keying moves them away from the identity the runtime
   computes: `via: media` (media URLs are fetched verbatim, signed params
   included), `via: extract-asset` (the key is a repo path, which
   canonicalization would mangle into `https:enrichment/…`), and any entry
   whose URL is not an absolute http(s) URL. Canonicalization is guarded
   per entry, as in migration 1 — one unreadable URL is skipped-with-why
   and keeps its stored hash, never aborting the chain part-way through.
   The rewrite is atomic and idempotent — a second apply finds every
   identity already current.
   Then, reruns: two known-deficient cohorts get their existing
   **URL-keyed work units requeued** (`status: queued, rerun: true,
   via: migration-2`) — real URLs through the front door, never item-keyed
   pseudo-entries:
   - every **web** item enriched before the link-keeping fix (stored
     enrichments have hyperlinks stripped);
   - every **x** item enriched before thread walk-up existed (stored
     enrichments are single posts that may have been threads; the rerun
     walks up and re-harvests, so e.g. a thread's YouTube link now becomes
     a child and gets transcribed).
   Only `done` entries are seeded — old `error` entries already retry under
   the new-engine rule, and `manual` entries stay parked for judgment. And
   only entries whose work a **live corpus item still claims**:
   `dex exclude` deletes the item and its enrichment while its ledger
   history stays on file, so a seed keyed to a purged item would re-fetch
   content ruled out of scope and put an owner ruling back in the queue.
   The claim is asked of the corpus, never of the entry's stored `item`
   string alone (amended at phase-4 review, on real state): an item RENAMED
   since its line was written — same shortid, new slug — has a live file
   under a new id, and a stored-string check reads that as a purge and
   refuses a rerun the owner never ruled out. So the entry's own item
   answers first where its file exists; otherwise the entry whose URL a
   live item still lists belongs to THAT item and is seeded **re-attributed
   to it**, the report counting the re-attributions. Only an entry no live
   item claims is skipped-with-why, naming the `state/exclusions.tsv`
   record where there is one ("excluded — never reseeded") and otherwise
   stating what was checked — the missing file, the absent exclusions row,
   the unclaimed URL — instead of asserting a cause it has not established.
   The re-key pass already ran, so a qualifying entry's hash and URL are
   the current identity: the seed appends under them directly, corpus
   seeding dedupes against it, and the drain fetches once.
   The draining session re-fetches with current code (stored text is the
   fallback for URLs now dead) and completes the cognitive steps from the
   run report, same as any capture. Legacy items whose `urls:` lists carry
   hand-walked thread parents keep them (frontmatter is immutable
   provenance, even where the old process polluted it); each such URL
   reruns and walks up independently — accepted chain-context duplication.
   **Cohort pacing is automatic, in code** (owner ruling at final review —
   "it's either automated or it's not"): each full run drains all
   non-rerun work first, uncapped, then `rerun: true` entries up to a
   50-per-run cap; the report states the cohort position ("rerun cohort:
   N of M drained; R queue for the next run"). No `--limit` babysitting —
   a migration cohort heals over the normal scheduled cadence with each
   session's cognitive load bounded. Explicit `--limit` still binds the
   total when passed.

3. **`cache/` gitignore for pre-existing instances** — appends the line to
   the instance `.gitignore` when absent (append-only, idempotent, reported
   as an action on an instance-owned file). Without it, in-flight audio
   shows as dirt to the dirty-tree guard.

Cap-event surfacing, blessed precisely: harvest-time cap fires stay off
every user surface; an owner-requested `enrich fetch` refusal IS surfaced
(the owner asked and deserves the answer); media-cap and oversize skips
surface as ordinary unit outcomes per the media-stage rules.

Resurrected transcription backlogs are bounded: the transcribe drain takes a
per-run cap (default 10) so a first sync never monopolizes a machine.

## 13. Issue filer: decentralized self-healing

Instances auto-file engine bugs at the public engine repo; the owner's Claude
session fixes; Mint releases; every instance heals at next sync. Human
involvement: the engine owner only.

- **Fires only on `status: error`** (deterministic engine exceptions).
  `blocked/dead/manual/waiting` never file — the world misbehaving is not an
  engine bug.
- **Sanitized by construction — public issue bodies carry NO free text**
  (owner ruling at final review): the body is engine version, command,
  kind/format enums, error class, `errno` where present (the one mechanical
  detail an OSError safely yields), engine-frames-only traceback, URL
  *hash*, fingerprint — and nothing else. The exception message is not a
  field at all: no mechanical scrubber can distinguish quoted owner-content
  from legitimate diagnostics, so the residual is eliminated structurally.
  The scrubbed message lives only in the local ledger's `error` field —
  diagnosis happens where the data lives.
- **Fingerprint = error class + function name + kind/format.** Excludes
  version and line numbers (those churn per release and would mint duplicate
  issues). Version data lives in the issue body.
- **Dedup**: fingerprint hash in the title; search before filing. Open →
  one "seen again (engine X, date)" comment per engine version per instance.
  Closed → silent if the instance's engine predates the fix (sync cures it);
  a recurrence on a *newer* version files a fresh issue referencing the old
  one (regression).
- **Local memory** `state/issue-reports.jsonl` — prevents re-spam, and is
  the owner's visible record of what their instance reported.
- **Rate limit**: max 3 new issues per run.
- **Fire-and-forget**: instances never poll issues or act on tracker
  content — the fix loop closes through releases and sync only. The public
  repo cannot instruct instances.
- **`gh` is assumed** — it's a declared instance dependency; an environment
  without it is not a dex environment (the Cowork VM class was discarded for
  exactly this kind of lack). No pending-file/retry machinery. The filer's
  only soft edge: its own failures are wrapped and non-fatal — the report
  notes "issue filing failed" and the run continues.
- Config: `report_issues: true` (default) in `state/config.json`. Filings
  appear in the run report, so the human always knows.

## 14. Module layout and dependencies

```
src/dex_engine/
  pipeline/    types.py  ledger.py  detect.py  registry.py  run.py
               ownership.py — which live corpus item claims a work unit
                 (its urls:/media: hashed exactly as seeding does), the one
                 answer to "is this entry's item still there?"; lint and
                 migration 2 share it so a renamed item cannot read as a
                 purge in one place and a rename in the other
  drivers/     youtube.py  x.py  github.py  paper.py  podcast.py  web.py  file.py
  capabilities/
    transcribe/  whisper_local.py  whisper_api.py
    extract/     anydoc.py  csv_builtin.py  cognitive.py
    ocr/         cognitive.py
  atomic.py    the ONE atomic-write implementation (same-dir temp file,
               then one replace; no temp orphan on failure) — every state
               write shares it: ledger, corpus items, the pin file,
               capture pointers, cached audio, migration rewrites
  corpus.py    the ONE corpus-item frontmatter read/write point: a frozen
               `CorpusItem` dataclass, parse/serialize that passes the
               Claude-authored body through byte-exact. Replaces the old
               regex edits; used by `item new`, the status/enrichment
               refresh, and migration 1 (which needs it regardless — a
               reason this ships now, not later)
  render/      kernel.py  surfaces.py — ships its own entry point:
               `bin/dex render --file <payload>` (in-process for the
               engine's own reports, by command for the skills)
  migrations/  migration_1.py  migration_2.py  …
  enrich.py    thin CLI: run · status · transcribe · fetch · compact
               · mark <url> <status>   (heals/manual resolutions write the
                 ledger through a verb, never by hand-appending JSONL)
               · pass <item> --stage …  (records stage completions in
                 passes.jsonl — same rule)
               · item new <capture-file>  (creates the corpus item: id
                 computed per the id rules, provenance stamped from the
                 capture, body = the note verbatim. Corpus-item creation
                 was always mechanical work — the only judgment is the
                 scope check before it. Handles both id paths: url-hash
                 and materialized-media directory; a capture still
                 carrying `asset:`/`name:` frontmatter is refused loudly —
                 `dex inbox` has not run, and creating a text item would
                 drop the binary's provenance silently)
  normalize.py imports shared detect/types (private kind_of copy deleted)
  inbox.py     materialized files feed the pipeline (format detect → extract)
  lint.py      grows checks: ledger schema, ledger↔tree referential
                 integrity (items with no corpus file — excluded-on-record
                 told apart from vanished; done outputs missing on disk),
                 waiting cohorts, pass records
  sync.py      grows: pin resolution, re-exec, migration runner, sync report
```

Dependencies: `trafilatura` (extraction only), `yt-dlp`, **`firecrawl-anydoc`**
(+), **`faster-whisper`** (+, packaging pending — §16), `pypdf` (−, replaced
by anydoc). License: MIT (LICENSE + pyproject, in place; all deps
MIT-compatible — AGPL options were rejected for this reason and anydoc made
the question moot).

**Implementation standards** (from the Python craft skills in
`.claude/skills/`; binding):

- **Day-zero rule (owner ruling, 2026-08-20): no module is grandfathered.**
  This is a spiked-and-proven system being rebuilt as a properly engineered
  one — there is no legacy, no Chesterton's fence, no user but the owner.
  Proven *behavior* is preserved (the inbox release-asset sequence, API
  shapes, the exclusions contract — knowledge that was paid for on real
  devices); its *expression* has no tenure. Every module the rewrite
  touches or keeps — inbox, normalize, lint, sync, exclude, new included —
  ends at the same standard as the new packages: typed, constructor-
  injected, argparse'd, tested. When in doubt between adapting old code
  and rewriting, rewrite — code is cheap; the standard is the asset. The
  engine ships uniform, never 80% modern / 20% legacy.

- **No import-time globals.** The old code's `ROOT = Path.cwd()` and
  config-read-at-import (behind a silent `except`) are the named
  anti-pattern. Two frozen dataclasses instead: `Instance(root)` exposing
  `ledger_path`/`enrichment_dir`/`cache_dir` as properties, and `Config`
  (typed fields; `media_fetch` becomes a `MediaFetch` StrEnum) parsed
  loudly in the CLI entry point — defaults only for a genuinely missing
  file. Constructor-injected everywhere. `today: Callable[[], date]` and
  `engine_version: str` are injected into the ledger/run layer — the §15
  date-stamping and retry-on-new-engine tests are unwritable otherwise.
- **Strict tooling from day 1**: ruff (line length 100, Google docstrings
  on public functions) + a strict type checker (ty or mypy) in the
  definition of done. Fresh codebase; no incremental-adoption excuse.
- **CLI = argparse subparsers**, zero business logic in CLI modules: parse,
  build `Instance`/`Config`, call the pipeline. (The old hand-rolled parser
  silently ignored typo'd flags.) Applies to `sync.py`'s migration runner
  too.
- **Synchronous by design.** Politeness delays serialize the pipeline
  anyway; async is out of scope, permanently — recorded so a future
  "performance improvement" doesn't import that pitfall list.
- **One public surface**: `pipeline/__init__.py` re-exports types/enums via
  `__all__`; dependency direction is types-import-nothing, only
  `registry.py`/`run.py` import drivers — which structurally prevents the
  normalize/detect/drivers circular-import trap.
- **Packaging**: `[dependency-groups]` (PEP 735) with `test = [pytest,
  pytest-cov, hypothesis]`, `lint = [ruff, ty]`; `uv.lock` committed.
  Hatchling stays — the `force-include` of `instance/` into the wheel is
  load-bearing; do not "modernize" it away. Pytest config:
  `--strict-markers`, default `-m "not live"`, `live` marker registered —
  hermetic-by-default everywhere, live tests opt-in.
- Lazy-import heavy deps inside providers (the existing `import yt_dlp`
  inside-function pattern — keep it) so CLI startup is unaffected.

Skill changes shipping with this:

- **Capture and processing are separated — one route in, one route
  through.** New skill `dex-capture`: given a link/file/note in any session,
  write the capture file into `inbox/` exactly as the phone shortcut would
  (binaries filed as `bin/dex inbox` would file them), **commit and push**
  (capture isn't finished until it's on the remote — captures are commits),
  confirm, stop. Seconds; never processes. The old "session-handed items
  skip the inbox" path is deleted — it was a second route in. Processing is
  always `dex-run`, in full ("process now" / "process the inbox" / the
  schedule); there is deliberately no process-just-this-item path.
  The per-item procedure moves from the `dex-ingest` skill into
  **`dex-run/references/ingest-item.md`** — subordinate material loaded via
  progressive disclosure when the run reaches per-item work, alongside the
  existing schema/state-format references. User-facing skills: `dex-capture`
  and `dex-run` (plus `dex-query`, `dex-lint`). Bare `/dex-capture` asks
  "capture what?"; with content in hand it just captures; `dex-run` takes
  nothing.
- **Skill authoring style**: deterministic behavior lives in code, never
  prose; order-critical sequences stay prescribed *with their reasons
  attached* (the "commit immediately — the repo copy is now the only copy"
  pattern) so deviations protect the invariant, not the letter; judgment
  work is described as goal + quality bar + boundaries (anti-improvisation
  rules stay), not scripted with gates. Written for whatever model an
  owner's scheduled task runs — the safety comes from the layers below
  (code constrains, lint verifies), not from prose density.
- **Ingest becomes report-driven, not inbox-driven** — the structural change
  the rest depends on. Procedure: pull → `bin/dex inbox` → **`enrich run`,
  read its report** → per-item cognitive work for *everything the report
  names* — fresh captures, drained reruns, newly-drained waiting cohorts,
  and staleness-backstop orphans from `status`. The inbox is one source of
  work; the pipeline's output is the work list. The full session order is
  `bin/dex sync` **first** (pin bump + migrations before anything touches
  state), then pull → inbox → enrich run → report-driven cognitive work.
- Subject-rule harvest (§10); verbatim render rule (§11); `transcribe`
  command + model guidance and the per-run cap; heal procedure ends by
  running `enrich mark <url> <status>` (the sanctioned ledger-correction
  verb); migration-report review (§12).
- **Corpus items are created by `item new`, never freehand** — code writes
  frontmatter, Claude writes prose. The governing line: mechanize where
  the *values* are mechanical (item provenance comes from the capture
  file); stay freehand where the values are judgment (digests — `signal`
  and `topics` are the judgment, so a verb would add ceremony without
  removing a decision; lint verifies their shape instead). Frontmatter
  thereby converges on the same guarantee as the ledger: a malformed item
  can't be written, because structure never passes through freehand
  writing. Corollary, closing a drift current practice shows: **the item
  body is the owner's note verbatim and stays that way** — Claude's
  interpretive context (what a linked video is, how a thread relates)
  belongs in the digest, not the item body; thread context itself lives in
  the enrichment via walk-up. After creation, exactly two frontmatter
  fields ever change (`status`, `enrichment:` listing), both derived from
  disk, both written by corpus.py. The derivation rule: `status:
  enriched` iff `enrichment/<id>/` holds markdown files, and the listing
  is those filenames, sorted. Every run reconciles **every** item against
  disk — not just the units it drained — writing only on change, so
  enrichment written outside the drain (media descriptions, cognitive
  heals) converges at the next run; `enrich mark` and `enrich pass`
  refresh their owning item in the same call, so frontmatter is already
  true when the session that wrote the file ends.
- **Wiki frontmatter's derived fields are lint-repaired, not carefully
  authored**: `lint --write` reconciles `items:` counts and `generated:`
  dates mechanically at health checks (the field the 2026-08-20 analysis
  found drifting on 16 pages). Wiki *bodies* remain the most freehand
  artifact in the system, on purpose — synthesis is the judgment.
- **Shipping obligations in the same change**: `dex-contract.md` updated
  (operations become capture/run/query/lint; dataflow adds `cache/`, the
  new state files, and ledger-only promoted URLs; the "never hand-edit
  corpus items" invariant is amended to sanction *migrations* writing
  engine-owned frontmatter fields — migration 1 rewrites `kinds:`). Note
  for migration 1's author: `normalize` regeneration re-derives kinds via
  the shared detect module, so rewritten frontmatter converges with
  regeneration rather than fighting it — including its ORDER. Normalize
  emits `sorted({kind_of(url) …})`, so the migration emits kinds sorted
  and deduped too; anything else and the first regeneration after the
  migration rewrites those items again for ordering alone (82 real corpus
  files), burying the migration's own commit in noise. Per the repo's anti-drift rules:
  pyproject entry points, the shim usage line, the README command table,
  and `docs/capture.md` + `docs/start.md` (dex-capture changes the capture
  story) all move together.
- **Wiki reprocessing rules**: for a re-fetched item (same id), grep pages
  citing that id and *revisit those sentences* — rewrite in place where the
  content changed, never splice additions for an already-cited id. Same fact
  from a new source → **add the citation to the existing sentence, don't
  write a new sentence**. Prevents restated-fact drift from reruns and
  promoted children.
- Lint: new state checks (ledger schema, waiting cohorts, pass records), a
  fuzzy same-page sentence-similarity flag (difflib, no models) surfacing
  "possible restated fact — merge?" warnings at health checks, and a
  **page item-count consistency check** (frontmatter `items:` vs the
  page's topic/entity MEMBER count — never its citation count; the wild
  convention and the "16 drifting pages" figure are both member-semantics —
  16 pages were silently drifting when the 2026-08-20 analysis looked,
  caught by accident rather than mechanism), and a **shortid-shaped
  citation flag**: backticked 6-hex shortids are probable malformed
  citations everywhere — index included — not just where full-id resolution
  happens to fail (a 2026-08-20 production run found latent shortid
  citations in an index that had never tripped the check because no page
  existed to fail against; the ingest reference states it plainly:
  citations are full item ids, always).
- **Unattended sessions never edit `state/config.json`** (or other
  owner-editable config): they surface the proposed change in the run
  report with its rationale; the owner ratifies in an attended session.
  Learned 2026-08-20: a scheduled run added domains to `internal_domains`
  unattended — a reasonable call made by the wrong authority.
- Ingest-reference guidance: navigational/index URLs (site roots, topic
  indexes) usually fail scope — their substance lives in their pages, which
  is the subject rule's job when the substance is wanted, and a skip when
  it isn't. Don't enrich navigation into empty items. (Judgment, not a
  blanket rule — a small site's root can be the content itself.)
- **Model recommendation**: skills can't pin a model — scheduled tasks can.
  dex-run's first-run setup recommends the owner set the task's model to
  **Opus-class or above**. The system isn't Claude-exclusive (any agent
  runtime that reads skills could drive it) but is untested elsewhere; the
  skill says so rather than pretending portability is verified.

## 15. Testing

The Protocol/pipeline split exists partly to make the system testable; the
old monolith had no seams.

- **Hermetic driver tests** against `tests/fixtures/` (fxtwitter JSON, GitHub
  payloads, VTT, tiny real docx/pdf/csv). Regression pin on the motivating
  incident: a 403 fixture must produce `blocked`, never `dead`.
- **Pipeline tests with fake drivers**: children/reruns born `queued` and
  drained, re-entry + provenance, caps fire and record, waiting ignores runs
  until a fake provider flips `available()`, blocked→manual escalation,
  error retry-on-new-engine, rerun overwrites (never duplicates) output.
- **Corpus round-trip property** (hypothesis): parse∘serialize over
  generated items preserves the body byte-exact and the frontmatter
  semantically — the guarantee that lets migration 1 and the refresh path
  touch thousands of Claude-authored files safely.
- **Ledger invariants as hypothesis properties**, not single examples:
  random entry sequences ⇒ `compact()` preserves last-per-hash and
  round-trips; ledger + identical rerun ⇒ byte-identical outputs, no new
  non-audit lines; `canonical(canonical(u)) == canonical(u)` over generated
  URLs (canonicalization keys the ledger hash — a non-idempotent case IS a
  duplicate-entry bug).
- **Render kernel tests**: the column/wrap math.
- **Live tests opt-in** (pytest marker) for real-API drift checks. CI runs
  hermetic only (enforced by the default `-m "not live"`, §14).

Suite structure: `tests/` mirrors `src/dex_engine/`
(`tests/drivers/test_x.py`, `tests/pipeline/test_ledger.py`,
`tests/render/test_kernel.py`; payload fixtures under
`tests/fixtures/<driver>/`). `tests/conftest.py` provides an
`instance(tmp_path)` fixture building the corpus/state/enrichment skeleton
(trivial once `Instance` is injected), a `FakeDriver` factory, and a
`FlippableProvider` whose `available()` the waiting-cohort tests toggle.
Driver/provider Protocol conformance is verified by the typed registry
literal (§2) — no separate conformance tests needed.

## 15a. Implementation order

Sequencing constraints are real; build in this order, each phase leaving
the tree green:

1. **Foundations** — `pipeline/types.py` (enums, dataclasses, validation),
   `pipeline/ledger.py` (from_line/to_line, compact, invariant tests),
   `corpus.py` (CorpusItem parse/serialize, byte-exact body round-trip),
   `render/kernel.py` + `surfaces.py` (everything downstream reports
   through them), tooling (ruff, type checker, pytest config,
   dependency-groups, uv.lock). Hypothesis properties land here.
2. **Detection + drivers** — `detect.py`, `registry.py`, `run.py`; port
   youtube/x/github/paper/web to the driver shape (x gains walk-up, web
   keeps links + classified fetches); media stage; fixtures per driver;
   `normalize.py` switched to shared detect; classification module with
   the 403-regression pin.
3. **Capabilities** — extract (anydoc, csv-builtin, cognitive), transcribe
   (whisper-local, whisper-api with chunking), ocr (cognitive); waiting
   queue semantics; `file` + `podcast` drivers (they depend on
   capabilities); `enrich fetch` / `mark` / `pass` verbs; capability
   report surface.
4. **Releases + migrations** — `.dex-engine-pin`, sync flow (re-exec,
   migration runner, sync report), Migration protocol + report review;
   migrations 1 and 2 (in that order — renames before requeues).
5. **Periphery + uniformity** — issue filer; skill rewrites (dex-capture,
   dex-run restructure with ingest-item reference, lint additions);
   **every remaining module brought to the day-zero standard with tests**
   (inbox, normalize, lint, sync, exclude, new — behavior preserved,
   expression rewritten; nothing ships legacy); dex-contract.md, README,
   docs/ updates per the anti-drift obligations; instance rollout (sync
   each instance, verify migration reports).

Phases 1–3 are pure engine work with no instance impact; nothing ships to
instances until phase 4 exists, because the first synced release must carry
the migrations that make old state valid under the new code.

**Version precondition** (phase-4 review, F1): the package version must be
bumped past every released tag before the stack merges — a running version
equal to an existing release makes the pin bootstrap adopt that (old) tag
and brick the instance on pre-rewrite code. The pre-rewrite marker
`0.0.1` must never be re-tagged or re-stamped.

**MERGE GATE — binding on the stack**: instances track `main` HEAD today
(the shim pins nothing until phase 4), so `impl/*` branches must NOT reach
`main` before phase 4's migrations and the pin mechanism exist. Landing
phase 2 early would make every instance's next `bin/dex` run fail loudly:
`ledger.load` rejects pre-migration vocabulary by design, and the docs/
skills still describe deleted verbs until phase 5. The stack merges to
main as a whole, or at minimum from phase 4 downward, never bottom-first.

## 16. Deferred / out of scope (tracked on the roadmap)

Instagram driver (shape not
agreed) · X thread walk-down · hosted
transcription provider investigation (Groq et al) · resurfacing / reading
queue · source removal · per-instance context instructions · S3/R2 media
storage · ideas/backlog system to replace the roadmap file · engine OCR
providers.

Verify at build time: anydoc wheels for macOS arm64 + Linux aarch64;
faster-whisper/CTranslate2/av wheels for the same platforms; uvx resolution
behavior for pinned tags (cache freshness); current Groq pricing before
documenting a recommendation.

**faster-whisper packaging — DECIDED (2026-08-20): core dependency.** The
system isn't useful if it can't transcribe; the always-available floor
holds on every instance unconditionally. Costs are layered so idle
instances pay almost nothing: wheels (tens of MB: ctranslate2, av) download
once per release per machine at uvx env build; the Python module loads only
when a transcription job runs (lazy import, §14); the model (~500MB for
`medium`-class) downloads only on first actual transcription.

Implementation-time care note: dex-run's every-run procedure has a
dirty-tree guard; sync now edits state (pin bump, migration seeds) before
that guard would run — sequence the guard relative to sync deliberately so
legitimate sync-produced changes don't trip it.
