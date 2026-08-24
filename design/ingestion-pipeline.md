# Driver-based ingestion pipeline

Status: **in force, implemented** (designed and signed off 2026-08-19/20;
the rewrite has landed). This document is the reference for the engine's
ingestion machinery and describes what IS. It is also the one design surface
for this release: work that is agreed and not yet built lives here too,
marked as such where it belongs — the driver-outcome contract (§2), the
cleanup round (§16), and the open questions their own sections state. The
roadmap holds no design; it indexes this file and notes what comes after the
release.

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
(hand-fetching the pages) left the ledger contradicting the corpus.

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
  │ FETCH   │  driver.fetch(unit) → Outcome (§2) — what the fetch FOUND;
  └────┬────┘  the run layer's one total match decides the status
       │
       ├─▶ body + meta ──▶ WRITE   enrichment/<id>/<kind>-<hash6>.md
       │                           (deterministic name → reruns overwrite,
       │                            never duplicate)
       ├─▶ media urls ──▶ MEDIA    shared stage — see §7
       │
       └─▶ needs ──▶ WAITING       status: waiting + needs: <capability>
                                   drained when a provider is available (§6)

 Promotion re-enters the queue from OUTSIDE this loop: which links are
 primary artifacts of the item's subject is judgment (§10), so the session
 reads the enrichment and names them — `enrich fetch <item> <url>…` — and
 the engine admits them with provenance, under the caps.
```

Caps on re-entry (mechanical backstops, not targets): **max depth 4** (the
shared URL is depth 0), **max 12 fetched URLs per item**. They bound
re-entry and only re-entry: a captured URL comes in at depth 0 through the
front door, and what the owner shared is not a promotion for a bound to
turn away. The two differ in what may be done about them — the 12 is a
budget on how much of the web one item drags in, so `--force` exceeds it
deliberately (§10); depth is a hard bound on how far the queue may walk
from a shared URL and has no override, so a refusal at depth names no route
rather than offering one that does nothing.

A fired cap is recorded in the ledger (`cap` on the skipped line, naming
the bound that refused the work) and internal logs, and both surfaces read
it. **The run report answers whoever asked**: every promotion is a URL a
session named with `enrich fetch`, so every refusal comes back on that run
report, with the `--force` route where the bound has one. **The health
check reads the same fires as a tuning signal** — how many, spread over how
many items, under which bound, worst offenders first — so the owner can
tell a corpus the bounds are too tight for from harvest judgment promoting
links the subject rule would not. The bound is counted off the typed `cap`,
never off the prose that worded it: one bound said two ways would otherwise
read as two bounds.

What splits the drift reading is the **override, not who asked**. A
refusal that stood is drift to read, whether the session promoted the link
on the subject rule or the owner named it by hand — the bounds hear the
same thing either way. A refusal the owner then waived with `--force` is a
decision already taken, so it leaves the reading; it is read off the typed
`forced` for the same reason the bound is read off `cap`. Counts and a
listing, never an alarm: a fire is a reading, not a fault, so it never
fails the check.

**What the 12 counts is fetched pages, and only those.** The count is over
the item's ledger entries, excluding every entry carrying a `job` —
`media` (downloads, not pages) and `asset` (bytes written out of a
container, which never touched the network) — and every `skipped` entry
(cap-fire markers record refused work, so counting them would let one
refusal ratchet the cap shut). A PDF
carrying thirty embedded images therefore spends one of the item's twelve,
not thirty-one — the budget bounds how much of the web an item pulls in,
and the media caps (§7) bound the rest separately.

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
died between fetch and digest), never the handoff mechanism. It lists only
items that **owe no further work**: an item holding a `manual`, `error`,
`blocked` or `waiting` unit derives `raw`, and a raw item is one the ingest
procedure forbids digesting — listing it would name the same permanently
parked item as work to do on every run, forever.

**Session-end invariant**: everything processable is processed — items are
digested whole, after their children land. The only entries that survive a
session are `waiting` / `blocked` / `error` / `manual`, each parked for a
stated reason, each printed in the report.

The report that prints them is the report of the run that parked them. A
unit that outlives that run is untouched standing state, so it is `enrich
status` that names it — id, URL, status and stated reason, split by who owns
the next action, exactly as the run report splits its own. Counts alone
(`**manual** 1`) left a long-standing park on no surface at all while its
item sat permanently raw and permanently undigestable. Sourcing the run
report from every surviving entry instead would reinstate what this section
already refuses: the same parked item listed as work to do on every run,
forever.

**Mid-fetch kind discovery** (a server lied to HEAD; "this is actually a
PDF") — implemented on owner order after an initial unauthorized deferral,
and NOT as a child (early drafts said "child-re-entry"; a child is a
different URL — this is the same URL, same hash, changing its mind): the
driver sniffs the fetched body (magic bytes first, via the central
detection module — bytes decide, never content-type alone) and returns
`Redetected(kind, format)`, identity only — the one outcome that
re-enters the queue. The run layer appends
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
digits, so two units under one item can collide — a `web-…`/`file-…` pair
under one hash6 has been seen in a live instance — and a name-pattern
unlink deleted
the neighbour's enrichment while its ledger line still read `done`. Nothing
is dropped for a replacement that is not itself on disk. Works in both
directions (web→file, file→web), and carries two further discoveries that
only a fetched body can make: a page whose audio is its subject, fetched
by the catch-all → `podcast` (§9), and a GitHub blob whose bytes are a
document → `file` (§3). Loop guard is per-run state: one correction per
hash per run, then `manual` "re-detection loop"; across runs a unit may
redetect again — the world changes.

The run report also derives a listing for **no-source items** (text-only
and image-only captures — no URLs, no work units): they surface as
cognitive work ("awaiting description + digest") so a capture with nothing
to fetch is never invisible to the session.

**An incomplete item says so on the report.** Every item the run touched
that still owes work gets a line stating the shape of it — "3 of 4 units
landed — 1 waiting on transcription" — so completeness is read, never
inferred from a list of units. Items the run did not touch are the standing
view's job (`enrich status`), not the run report's.

**Enrichment file format.** Every fetched output is one markdown file,
`enrichment/<id>/<kind>-<hash6>.md`, opening with a YAML frontmatter fence
whose first two keys are always `url:` (the canonical URL the unit was
keyed on) and `fetched:` (the run date), followed by the driver's `meta`
verbatim, then the body. Empty and null meta values are dropped rather
than written blank. Values are quoted defensively against **YAML 1.1
retyping**, because a plain scalar that merely *looks* like something else
comes back as that something else: `no`/`off`/`on` and friends read as
booleans, a leading `-`, `?` or `,` or any of the YAML indicator
characters changes the parse, a tab anywhere invalidates the block, and a
date-shaped or number-shaped title reads back as a date or a float — so a
scalar matching any of those shapes is emitted as a JSON string instead.
The shape match on timestamps is deliberately on shape alone, never
calendar validity: `2026-08-99` is what makes a YAML reader *raise*, so it
must be quoted too.

The `fetched:` stamp is **masked out of the rerun byte-compare** — it
changes every run by definition, so comparing it would report every rerun
as changed and rewrite every file. Only the frontmatter head is masked; a
body line that happens to begin `fetched: ` is content and counts as
change. Two documented mechanics rest on this: the rerun "changed" list on
the run report (§12), and the enrichment-newer-than-digest backstop.

**Normalize is contained per channel, and the containment is split between
the read and the write.** `raw/discord/<channel>/messages.json` is one
export among many and one bad export must not cost the rest, so a channel
whose file cannot be read at all — truncated mid-write, not UTF-8, another
tool's JSON — is named on its own summary line and skipped while every
other channel normalizes. Inside a readable export, a single message the
code cannot read (a missing field, a timestamp that will not parse, a
container that arrived as a string, a display name the corpus cannot take
as a scalar) is skipped and counted **per reason**, one line per reason
rather than one per message: a conversion that drops a field drops it on
every message, and one line says so.

The read/write split is what makes "skipped" true by construction rather
than by hoping validation caught everything. The whole read — parse, drop
the messages that cannot be read, cluster — finishes before the first item
is written, so a channel that faults there has written nothing and
"skipped" is the whole truth about it. Only the read is contained that way.
A fault in the WRITE is a different fact about the corpus — some of the
channel's items are on disk and the rest are not — so it is reported as
exactly that, with the count that did land, as `channel incomplete`, and
the run finishes so the report still reaches the operator with a channel to
re-run. The alternative is the failure this replaces: a calm "skipped" over
a part-normalized channel whose items are already on their way to
enrichment and the wiki.

## 2. Interfaces

`typing.Protocol` throughout (structural interfaces; engine floor is 3.11).
Two families, because two different things are swappable:

```python
class SourceDriver(Protocol):          # one per source shape
    kind: Kind
    sleep: float                       # per-driver politeness delay
    def matches(self, url: str) -> bool: ...
    def canonical(self, url: str) -> str: ...
    def fetch(self, unit: WorkUnit) -> Outcome: ...

# What a fetch FOUND, as a closed union — no variant carries a Status
# (lifecycle is the run layer's decision, below), and outputs exist only
# on Content, so the wrong states are unrepresentable rather than
# validated:
Outcome = Content | Missing | Refused | Unusable | NeedsCapability | Redetected

@dataclass
class Content:                         # fetched something — the outputs
    meta: dict                         # passthrough → enrichment frontmatter
    body: str | None
    media: list[str]                   # URLs for the media stage
    assets: list[Asset]                # extraction assets (bytes) — drivers
                                       # never touch disk; the run layer
                                       # writes them under the §7 caps,
                                       # ledgered job: asset

@dataclass
class Missing:                         # confirmed gone
    evidence: str                      # the driver-stated reason rides the
                                       # union as required `evidence` on the
                                       # three failure variants; the run
                                       # layer may append context but never
                                       # invents what the driver knew

@dataclass
class Refused:                         # blocked, paywalled, rate limited
    evidence: str
    permanent: bool = False            # the driver-known fact that a retry
                                       # can never change the answer
                                       # (payment/login walls, geo-blocks)

@dataclass
class Unusable:                        # wrong shape, nothing to extract
    evidence: str
    rescuable: bool = True             # False when the URL addresses no
                                       # content unit at all (a site root,
                                       # an index) — nothing for judgment

@dataclass
class NeedsCapability:                 # needs transcription, extraction, OCR
    need: Need
    meta: dict                         # partial content already in hand —
    body: str | None                   # a description, show notes, the
                                       # enclosure pointer — written now,
                                       # completed by the capability drain
    reason: str | None                 # the stated park, for the report

@dataclass
class Redetected:                      # this is not what we thought it was
    kind: Kind                         # (§1 mid-fetch kind discovery);
    format: Format | None              # identity only — the corrected
                                       # kind's driver owns the outputs

@dataclass
class WorkUnit:                        # what the pipeline hands a driver
    hash: str                          # sha1(work key)[:10]
    url: str                           # canonical URL, or file:<repo-path>
    kind: Kind
    format: Format | None              # file work only
    item: str                          # owning corpus item id
    depth: int                         # 0 = the shared URL
    parent: str | None                 # spawning work unit's hash

# A driver returns content, media, assets and metadata — never
# links-to-promote. Promotion is judgment, and it enters through
# `enrich fetch` (§10), where the PIPELINE assigns depth = parent depth
# + 1, parent = the named unit's hash, and the item from the command.

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
  `Content` gets defaults (`media`/`assets` via `field(default_factory=
  list)`, `body` None) so a simple driver returns
  `Content(meta=…, body=…)`. `meta` is `dict[str, str | int | None]` (it
  becomes YAML frontmatter), never bare `dict`. A failure variant's
  `evidence` is required and non-empty — an unexplained park is the
  stated-reason contract's forbidden state, unconstructable.
- **WorkUnit is not LedgerEntry.** Drivers see url/kind/format/item/depth —
  never `attempts`, `engine`, `rerun` bookkeeping (no internal types across
  layers).
- No outcome carries a `Status`, and `error` is raised, never returned (no
  variant has an error channel; the pipeline's single broad except is the
  only route to `error` status). `queued` is a birth state: `Redetected`
  is the one outcome that re-enters the queue, and it carries identity
  only.
- The typed registry literal (`build_drivers`'s `list[SourceDriver]`
  return) is the Protocol-conformance point — the type checker verifies
  every driver against the interface at that one return. Same for
  provider lists. The registry is built by that function on demand
  (§16), never at import.
- Every **total** dispatch over `Status`/`Kind`/`Need` uses `match` +
  `assert_never` (exhaustiveness: adding an enum member becomes a type
  error at every unhandled site). The drain predicate is one total
  function, `is_drainable(entry, ctx) -> bool`, exhaustively matched — the
  highest-value function in the system to make total. A dispatch that is
  deliberately **partial** — a Kind or Status that is a valid member but
  not applicable at that site (transcript bodies and audio acquisition
  cover youtube and podcast only; the acquisition-failure and classifier
  arms take the three statuses their classifier can return) — states the
  mismatch at runtime instead of defaulting quietly. Four do it by
  raising: the transcript-body dispatch, the acquisition-failure arm, the
  classifier arm, and `Classification.to_outcome` — the one conversion at
  the classifier seam, total over the three statuses the classifiers
  produce (dead/blocked/manual) and loud beyond them — each a loud engine
  bug when it fires. The audio-acquisition arm is the one that does not — it returns
  `Classification(status: manual, …)` naming the same mismatch, so the unit
  parks with the reason stated and the rest of the drain runs. The
  remaining `case _: raise` in the tree belongs to none of this: it is
  `enrich.py`'s argparse guard, unreachable while argparse enforces the
  command set.

Capability resolution: **per format / per need, first available *mechanical*
provider wins**, order set by instance config. The **cognitive provider** is
always last in resolution order and is the always-available floor for
judgment-shaped work — but `enrich run` never "drains" it: cognitive jobs
are listed on the run report, the session does them with eyes, and appends
the `done` entry. Precisely: **`waiting` means no mechanical provider**; a
job that resolves to the cognitive provider is `waiting` from the queue's
point of view and work-to-do from the session's. No instance ever hard-fails
on a missing key or an unsupported format.

### Driver outcomes: drivers report, the orchestrator decides

**Implemented** — the contract in force, and the shapes the §2 sketch
above shows. Drivers do not decide lifecycle status: they report what they
found, and the orchestrator decides what that means.

Before this, every driver returned a flat `Result` carrying a `Status`, so
each one decided the unit's lifecycle for itself. That was the root of the
largest family of defects the review rounds found, and it recurred in six
of them:

- private GitHub blobs classified `dead` because an unauthenticated fetch
  404s, condemning live content
- YouTube channel URLs driven as videos, enumerating a whole channel
- Apple podcast episodes unresolvable because the wrong id was looked up
- arxiv spellings splitting into separate work units
- `IncompleteRead` escaping as an engine bug rather than a retryable block
- a "listen to this article" widget rerouting an article to the podcast
  driver, discarding the article

Each was fixed where it was found (§3, §5, §9). The class survived because
there was no single place that decided what a fetch outcome means, and no
way to audit which statuses a driver may produce.

A driver is a black box over one source shape. It is passed a work unit,
does its job, and returns an outcome describing what it found:

    Content(meta, body, media, assets)  fetched something
    Missing(evidence)                   confirmed gone
    Refused(evidence, permanent)        blocked, paywalled, rate limited
    Unusable(evidence, rescuable)       wrong shape, nothing to extract
    NeedsCapability(need, meta, body,   needs transcription, extraction, OCR
                    reason)
    Redetected(kind, format)            this is not what we thought it was

The driver keeps the knowledge only it has. That a particular yt-dlp
message means confirmed gone, that a 402 means paywalled, that a thin
extraction is not content: those are per-source judgements and they stay in
the driver, carried as evidence on the outcome — plus the two typed facts
only the driver can state: `permanent` (a refusal a retry can never
change) and `rescuable` (whether an unusable fetch left anything for
judgment to act on). `Redetected` is the mid-fetch kind discovery of §1,
which was the one sanctioned shape in which a driver could return
`queued`; as an outcome it stops being a special case.

The run layer maps outcome to `Status` in one total match, in one place
(`_Drain._apply`, closed by `assert_never`):

    Content                       → done   (outputs written, media staged)
    Missing                       → dead
    Refused(permanent)            → manual (attempts would teach nothing)
    Refused                       → blocked (the attempts lifecycle, §5)
    Unusable(rescuable)           → manual
    Unusable                      → skipped (owes nothing, holds nothing)
    NeedsCapability               → waiting + needs (park file written now)
    Redetected                    → queued, superseding line, re-drained

`Missing` is the only road to `dead`. The mapping is auditable and tested
on its own (a run-layer pin per arm, mutation-checked), and the question
"can this driver produce a terminal status by accident" is answered by
this table instead of by reading seven drivers.

**Drivers never raise.** Every escape becomes an outcome at the seam. A
driver that raises is an engine bug, not a content problem, and the run
layer's single broad except (§5) records it as such. This kills the
exception-class defects outright rather than patching one boundary at a
time, which is what the provider-boundary round had to do for
`IncompleteRead`, `HFValidationError`, `IndexError` from a container with no
audio stream, and `csv.Error`.

**Drivers are isolated.** A driver never imports another driver. Behaviour
two drivers share becomes a lib beside `drivers/transport.py`,
`drivers/gh.py` and `drivers/audio.py`. **Settled — `paper.py`** (owner
ruling, 2026-08-24): the wholesale delegation to `WebDriver` ends. Article
extraction, the wayback rescue and thin-extraction handling are one shared
route, hoisted into `drivers/article.py` beside the other libs; web and
paper both consume it, and the isolation rule holds with zero exceptions —
the isolation test names none.

**What this replaced.** The flat `Result` — a dataclass whose fields were
only meaningful for some statuses — is gone without a compatibility path.
It could carry `media` on a non-done result: `assets` was validated
done-only while `media` was not, so the type admitted outputs on a result
that produced none, and nothing but convention kept them off. Making the
wrong states unrepresentable was the point, not a side effect — which is
also what settled the two sub-questions the sketch once left open: the
outputs (`body`, `media`, `assets`) ride `Content` and only `Content` (no
other variant has the fields), and the driver-stated reason rides the
union as the required `evidence` of the three failure variants and the
optional `reason` of a `NeedsCapability` park — `Content` and `Redetected`
carry none, because a success and an identity correction have nothing to
explain.

**Settled — `links` vs `children` on the outcome union.** Neither: the
outcome union carries no link-promotion field, because the deletion of the
never-emitted `children` path settled it — promotion is judgment (§10) and
enters through `enrich fetch`.

Two related shapes shipped with it:

- `LedgerEntry` and `WorkUnit` disagreed on what `depth = 0` means, so a
  whole class of legal ledger states could not convert: an entry paired
  `parent` with *having* a depth, a unit paired it with a **non-zero**
  depth, so `LedgerEntry(parent set, depth 0)` was legal and the same
  `WorkUnit` refused. One rule now, stated once (a shared lineage check):
  a spawned unit carries parent and depth ≥ 1 together, an original
  carries neither, each type keeping its own spelling of "original" — the
  unit's depth 0, the entry's absent depth.
- `via` carried provenance, routing (`via == "media"` dispatched) and a
  migration marker — three jobs, one field. Split: routing is the typed
  `job` field (§3, §5) and `via` keeps provenance and the migration
  marker, never dispatched on.

**Acceptance bar.** Each change must name the defect class from the review
record that it makes structurally impossible. Moving code is not enough and
line count is not a criterion. Secondary tests: the rule now lives in
exactly one place, the dependency graph is strictly simpler, and the tests
still mean something afterwards, proven by mutation rather than by passing.

**Not in scope**: inheritance for drivers, a plugin system, and coverage
targets. `matches()` and `canonical()` stay as they are — an ordered chain
of responsibility with a catch-all last, which is proven here.

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
| `github` | ✓ | repos / profiles / gists / issues / blobs. Every route goes through the authenticated `gh` CLI, blobs included — `raw.githubusercontent.com` is unauthenticated, so it 404s every private-repo blob however the machine is signed in, and that 404 classified live content `dead`. That route is a **shared seam** (`drivers/gh.py`), not this driver's property: the file driver reads blob bytes through the same module, so neither driver imports the other and the ref boundary below is resolved one way for both. Losing raw.githubusercontent.com costs the **ref/path boundary**, which that host resolved server-side and the contents API cannot: branch names hold slashes (`blob/automation/bors/auto/README.md`), and the `blob/refs/heads/<branch>/` permalink form spends two segments before the name even begins, so splitting at the first segment sent a wrong `?ref=` with a wrong path and 404'd live files into `dead`. The seam guesses the shortest ref the URL's shape allows — free, and right for nearly every link — and only when that 404s asks `git/matching-refs/<heads|tags>/<first segment>` which of the repo's own refs the path starts with, matching segment-wise (`automation/bors` must not claim a URL whose branch is `automation/bors-next`) and taking the longest. A repo with thousands of branches costs the same one page as a repo with three. When no ref re-splits the URL, or the lookup itself fails, the original classification stands: a genuinely missing path is still `dead`, never unclassifiable. A blob too large for the contents API to serve inline parks `manual`, never `dead`. github.com's **reserved first segments** (`/features`, `/topics`, `/sponsors`, `/orgs`, `/collections`, `/marketplace`, `/trending`, `/about`, `/pricing`, `/settings`, `/explore`, …) can be neither user nor repo, so the driver declines them and registry order hands them to `web`, which extracts them like any page — driving them as repo work 404'd pages that render fine in a browser into `dead`. Only the first segment is screened: `acme/topics` is an ordinary repo. **Blob bytes are sniffed — by filename — before they are fenced**: a document committed to a repo re-detects to `file` work rather than being fenced as source (a code fence full of replacement characters, ledgered `done`, was the incident), and any other binary parks `manual` naming what it is. The re-detection is only safe because the file driver shares the seam: fetching the blob URL over plain HTTP would find the HTML viewer page and re-detect straight back (a loop park on a public repo, `dead` on a private one), while HTML arriving from the contents API is a committed HTML file and never bounces. The **name** is what catches the two extractable shapes that carry no byte signature: a CSV, and an unsmudged **Git-LFS pointer**, whose 130 bytes of `oid sha256:…` decode as clean UTF-8 and fenced `done` as though they were the document they stand for (the contents API serves the pointer, never the object). Both re-detect to `file` like every other document — a committed CSV reaches an extractor as a real table instead of a fence truncated at 40k characters, and a pointer meets the file driver's LFS guard, which parks it `manual` before any extractor is handed 130 bytes of stand-in text |
| `paper` | ✓ | arxiv / openreview / hf-papers. A paper's identity is its arxiv id: every spelling — `/abs`, `/pdf` (with or without the `.pdf` tail), `/html`, a `v<n>` version suffix, a `?context=` listing tail — keys to `https://arxiv.org/abs/<id>`, one work unit per paper. **The version is dropped because the fetch is versionless**: the export API is queried by bare id and the HTML rendering is read from the bare id too, so both return whatever arxiv currently calls latest — keying `v5` separately would give one paper two ledger entries, two enrichment files and two copies of one abstract, all fetching the same bytes. Non-arxiv paper hosts keep the generic canonical: openreview and hf-papers pages fetch as articles through the web driver's fetch, the ledger kind staying `paper` |
| `podcast` | ✓ | new — Apple/Spotify/RSS episode links (§9) |
| `web` | ✓ | renamed from `blog`; registry catch-all, always last |
| `file` | ✓ | URL-served, repo-committed (github blobs, through the shared `drivers/gh.py` seam) or captured binaries; routes by Format |
| `image` | — | corpus-only: described cognitively at ingest |
| `text` | — | corpus-only: note-only capture, nothing to fetch |

**Format** — extractor routing only (not images, not audio):
`PDF, DOCX, PPTX, XLSX, ODT, ODS, ODP, RTF, EPUB, CSV`.

**Status** — `queued, done, dead, skipped, manual, waiting, blocked, error`
(§5).

**Need** — `transcribe, extract, ocr`. Needs are mechanical and
resource-keyed only. Cognitive obligations on *items* (re-judge under new
harvest rules, refresh a stale digest) are never queued — they are **derived
state**, computed on demand from state already committed (`passes.jsonl`
rules version vs the current constant; the item's digest pass date in
`passes.jsonl` vs the ledger's last `done` for the item). Mechanical
obligations are queued because they're work to be done; staleness is
derived because it's a fact that shows. **Both comparands are dates in
committed state, never file mtimes**: git stamps every file at checkout, so
on a second machine the whole tree shares one mtime and staleness becomes
undetectable. Day granularity is the intent — enriching and digesting in
one session is not stale. The digest file's own `date:` is the item's
*share* date, so it can never serve here.

**`via`** (provenance) stays a documented string, not an enum —
`harvest, sniff, migration-<n>` — because `migration-<n>` is parameterized
and provenance is descriptive, never dispatched on. Routing is `job`, a
typed StrEnum field (`media | asset`) marking the units that are not
driver-fetched pages: the run loop routes `job: media` entries to the
media redrain (§7 mandates media entries carry the parent's kind, so the
field is their only marker), and the fetched-page readers — the 12-URL
budget, the harvest obligation — ask it "is this a page". `via` once
carried that dispatch as its blessed exception; the split ended it, so no
dispatch reads prose-space anywhere.

**Lifecycle signals are typed schema fields, never prose.** `reason` is
display text: reports render it, nothing matches on it, and rewording one
can never change routing. The two signals that once looked reason-shaped
are §5 fields instead: a blocked audio-acquisition retry carries
`needs: transcribe` (routing it back through the transcribe drain, not the
driver), and a cap-fire marker line carries `cap: depth|url-requested`
plus `forced` when `--force` waived the bound. Both are typed for the same
reason: the health check counts the fires nobody overrode, by bound, and
counting either off `reason` split one bound across every wording of it —
and would have put a routing signal in the free-text namespace. This keeps
the
`reason` namespace safe for session-authored free text
(`enrich mark --reason …`), which must never be able to collide with a
routing signal.

## 4. State

**JSONL, ratified; SQLite rejected.** State travels through git between
machines (owner's desktop, other owners' laptops, scheduled sessions —
environments lacking dex's declared dependencies, such as the Cowork VM
class, are not dex environments at all, per §13). Git cannot merge a SQLite blob; JSONL merges as a union —
`.gitattributes` (synced) sets `merge=union` on **all `state/*.jsonl` files**,
every one of which is append-only. Diffability is
load-bearing (audit-by-git-log is free); Claude greps state directly in
sessions. If an instance ever outgrows this: SQLite as a derived, gitignored
cache rebuilt from the JSONL — the ledger stays the source of truth.

Rule: **state Claude reasons over is markdown (digests); state machinery
processes is JSONL/JSON; nothing binary in `state/`.**

Files:

| file | holds |
|---|---|
| `state/enrichment-ledger.jsonl` | work units (§5) |
| `state/passes.jsonl` | per-item stage records — `{stage: harvest, item, rules, date}`; "ran and promoted nothing" must be distinguishable from "never ran", and the health check reads both sides of that distinction (§10). The digest pass is recorded by `enrich item digest` itself, in the same call as the file (§10); `enrich pass` records the harvest and wiki stages, and remains the manual re-record for any stage |
| `state/migrations.jsonl` | applied-migrations log (§12) |
| `state/issue-reports.jsonl` | one record per issue report, filed upstream or gated (`filed: true\|false` says which — the `report_issues` gate stops only the filing, never the record); observed-report records also carry the local-only `note` (§13) |
| `state/digests/<id>.md` | per-item fact indexes; the one markdown corner of `state/`. Claude's judgment, the engine's shape: `enrich item digest --file <payload>` serializes it (§14). Removed with its item by `dex exclude` — the one thing that ever deletes one |
| `state/exclusions.tsv` | id + reason per purged item, written by `dex exclude`; excluded items stay excluded across re-normalization, and migrations consult it before reseeding (§12) |
| `state/config.json` | instance config — renamed from `normalize-config.json` (migration); holds `media_fetch`, `transcribe_model`, `transcribe_base_url`/`_api_key`/`_api_model`, `report_issues`, `internal_domains`, and provider order as `providers: {<capability>: [<name>, …]}`. `noise_prefixes` is accepted and reserved — nothing reads it yet. Unknown keys rejected loudly |
| `cache/` (gitignored) | ephemeral: render payloads, in-flight audio. Never state, never synced. Created at scaffold and ensured by every sync — a migrated pre-existing instance never scaffolded — since the per-item procedure renders every receipt through `cache/receipt.json` |

Ledger mechanics: append-only, full-record lines, **latest-per-hash wins**.
`enrich compact` rewrites keeping only the latest line per hash (also settles
union merges). Superseded lines until then are the audit trail. Compaction's
operating home is the health check: the dex-lint skill runs `enrich compact`
as a step, because no other operating surface ever schedules it and an
instance would otherwise never compact.

**Latest is by write time, not by file position.** Git's union driver
concatenates ours-then-theirs, so after two machines merge, the last line of a
hash is whichever side git appended — not whichever machine wrote later. So
every ledger line carries `at`, the UTC write instant with sub-second
precision, stamped by the one writer seam (`ledger.stamp`) from an injected
clock; `load` resolves each hash by `(at, file position)` and `compact` keeps
the line `load` resolves to. Lines predating the field carry no `at` and sort
oldest — every writer since stamps one, so an unstamped line necessarily
predates every stamped one, and no backfill is needed (stamping old lines with
a migration's own run time would be a lie, and they would all tie anyway).
Without this, a scheduled run's 09:00 `blocked` could land after the owner's
10:00 `manual` in the merged file, re-drain against the owner's decision, and
`compact` would then delete the newer line permanently.

**`at` is stamped by the writer, and never trusted past the present.** A
write timestamp is the writing machine's wall clock, and a wall clock can be
wrong, so two rules bound what a wrong one costs. On write: `at` is set by
`ledger.stamp` from the injected clock and is never carried in from a caller
— every write path goes through that seam, a migration's seeds included. On
read: a stored `at` more than five minutes
(`ledger.FUTURE_SKEW_ALLOWANCE`) ahead of the reader's clock — the wall
clock, injectable so resolution is testable — is not read as a write instant
at all. It is treated as unstamped, so it sorts oldest and loses to every
real write, and `compact`, which keeps exactly the line `load` resolves to,
drops it rather than the real writes behind it. A jumped clock therefore
costs one line its ordering and never the hash, and `mark` still heals,
because a heal is the newest real write. Without the ceiling, one line
stamped 2099 owns its hash until the wall clock catches up: `mark` reports
success, the item's frontmatter is rewritten from the in-memory drain and
reads healed, `load` goes on resolving the stale line, and `compact` deletes
every correct write behind it. The allowance is the ordinary spread between
two machines whose lines merge; a clock stepping backwards inside that
window can still mis-order two writes of one hash, and that is accepted.

**The ceiling bounds the future only, so a write is read back before it is
reported.** Nothing bounds a clock set BACKWARDS — a dead RTC, a container
with no NTP, a restored snapshot — because there is no reference point for
"too old", and in that direction the correction is stamped in the past,
where it loses to the very line it was written to supersede and `compact`
then deletes it as superseded with no audit trail. Strictly worse than the
forward case, which at least keeps the correction, and the verb reported
success throughout. So `mark` re-reads the ledger after appending and,
unless the hash now resolves to the line it just wrote, raises — naming the
URL, what the ledger resolves that unit to instead, and this machine's
clock as the likely cause. It fails BEFORE the superseded outputs are
dropped and the item's frontmatter refreshed, so nothing acts on a
correction the ledger did not take; the appended line stays as the audit
trail of the attempt. The drain's exit path makes the same check for `run`
/ `fetch` / `transcribe` and reports it as a note rather than raising,
because a run does real work and the rest of what it reports is true. The
resolution rule itself is untouched.

**Ownership is asked of the corpus, never of the line's stored `item`.** A
ledger line's `item` is the attribution as of the day it was written, and
migration 1 carries a stated item verbatim — so every line of an item
RENAMED since names an id no corpus file answers to, and an `exclude` that
keeps a line a surviving co-claimant shares leaves it naming the purged
one. The corpus is the source of truth, so the corpus is asked
(`pipeline/ownership.py`): the live items listing the URL, failing that
the parent's owners (a harvest-promoted child, a media download and an
extracted asset are in no frontmatter), failing that the stored string as
written. Every reader routes through it — the item's derived
`status`/`enrichment:` refresh, `enrich status`, the run report's parked
and incompleteness rows, the digest-staleness backstop (its landing
dates, and the candidates it derives from enrichment directory names: a
directory an interrupted rename left under the old id resolves to the
live item, so the backstop asks the right item for the digest),
`mark`'s post-heal refresh, `exclude`'s claim veto, and lint's ghost and
output rows.

**And so does the write path**, which is what makes healing unnecessary.
The drain carries an entry's item into the outcome line it records and
into the `enrichment/<item>/` path it writes, so it resolves the owner at
that moment from the same seam the readers use: the outcome line, the
enrichment file, a media download's directory and slot, the transcribe
drain's park file, a redetection's superseding line, and the `WorkUnit`
handed to a driver. Where nothing live claims the unit the stored string
stands, exactly as the read side falls back. Nothing rewrites a persisted
line to correct it: a stale `item` on an old line is historical
attribution, every reader and every writer resolves the work to the live
item anyway, and a session that renames an item has nothing to do about
its ledger. What a rename CAN leave behind is the enrichment directory,
if the session moved the corpus file and stopped — lint names that as a
`done` output filed where the item that owns it cannot list it, and moving
files is judgment work, not the drain's.

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
  "cap": "url-requested",      // skipped only: a cap-fire marker — refused
                               // work, not an admitted unit — naming the
                               // bound (depth | url-requested, the latter
                               // being what an `enrich fetch` refusal at
                               // the URL bound writes — the one road into
                               // the queue besides a capture)
  "forced": true,              // cap-fire markers only: `--force` waived
                               // that bound, so the unit entered anyway and
                               // this line is the audit trail behind the
                               // queued one. A waived fire leaves the
                               // health check's drift reading (§1)
  "engine": "0.2.1",           // engine version that wrote this line
  "date": "2026-08-19",
  "at": "2026-08-19T09:00:00.123456+00:00",
                               // UTC write instant, sub-second — what
                               // resolves latest-per-hash across a union
                               // merge (§4). Absent on lines written before
                               // the field shipped; absent sorts oldest
  "job": "media",              // which stage owns the unit's work when it
                               // is not a fetched page: media downloads
                               // and extraction-asset writes. Typed and
                               // dispatched on (§3) — the redrain and the
                               // fetched-page readers ask this, never via
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

**Every write records work, so every write stamps.** `date` and `engine`
answer a different question from `at`: when, and by which engine, the work
a line records was done. The writer seam (`ledger.stamp`) sets all three
from the injected clocks and the running version, with no carve-out,
because nothing writes a line that records no work. Both are load-bearing
rather than decorative — re-stamping `engine` on a line that ran nothing
would spend the retry-on-new-engine shot above without ever running it,
and `date` is the day the enrichment landed, which the digest-staleness
backstop reads. That rule is what rules out the obvious repair for a
stale `item`: a line re-recorded purely to correct its attribution would
have to carry both fields rather than claim them, and the write path asks
the corpus who owns the unit instead (§4). A line's stored `item` is
therefore never rewritten — it is the attribution as of the day it was
written, and history is not repaired.

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
  payment-required), and **any other status the transport surfaces →
  `blocked`**: a surfaced 1xx/3xx (a redirect loop's final 30x, an
  out-of-place informational response) reads "unexpected HTTP <n>", an
  unlisted 4xx reads plain "HTTP <n>" —
  the classifier is total; no HTTP outcome is unclassifiable. Routed
  through by every driver's HTTP path. The §15 regression pin tests the
  classifier once and holds for all drivers.
- **The transport seam normalizes `http.client`'s protocol failures into
  `OSError`** before any caller sees them, so the connection classifier
  covers them like any other. `IncompleteRead` — a server closing the
  socket mid-body, the routine failure mode for a 100MB enclosure — is an
  `HTTPException`, not an `OSError`, and would otherwise slip past every
  `except OSError` guard and land as `error` + a filed issue. A truncated
  read is a connection failure: **`blocked`, retried**, never an engine bug.
  The same guard wraps `dex-inbox`'s two urllib sites — an asset download is
  the same large-read shape — so a truncated read there is a stated
  `dex-inbox:` failure, not a traceback.
- **The transport seam puts a non-ASCII URL on the wire, it does not refuse
  one.** `http.client` encodes the request line as ASCII, so an accented
  path, a CJK slug or an IDN host raised `UnicodeEncodeError` — a
  `ValueError`, so it escaped every `except OSError` guard and parked the
  whole item `manual` with a raw codec dump as the owner-facing reason,
  before a single byte was fetched. The seam now encodes on the way out:
  IDNA for the host, percent-encoding for path, query and fragment, with
  `%` in every safe set so an already-encoded URL passes through
  byte-identical and the transform is idempotent. Canonical form is
  untouched — it keys the ledger, and re-encoding it would orphan every
  existing entry. Only a host DNS genuinely cannot carry fails, as a stated
  `ValueError` the bad-seed containment parks on with a readable reason.
- **An http-shared source may fall back to http at fetch time.**
  Canonicalization still forces https: identity is the ledger's key, and a
  scheme split would give one resource two entries. But a server that was
  shared as plain http may never speak TLS at all, so when the https
  attempt fails at the TLS layer and the item's own `urls:` show the
  source was shared as http, the fetch retries over http. A TLS failure on
  an https-shared source still classifies `blocked` like any other
  connection failure: the capture is the evidence of what the server was
  ever claimed to speak, and the fallback never downgrades a source the
  owner shared secure.
- **A truncated ERROR body never costs the status code.** The body of a
  4xx/5xx is only detail; the status is the finding. Both HTTP seams (the
  transport, whisper-api's multipart POST) drop a failed error-body read and
  return the status, because losing it inverts the verdict: a `404`'s `dead`
  would arrive as `blocked`, and whisper-api's `400` — the audio is bad,
  `manual` under the escalation clock — would arrive as an unreachable
  endpoint, `waiting` with no clock at all.
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
- **Two** `except Exception` in the whole pipeline, each named where it
  sits. The first is the per-unit
  loop in `run.py` (→ `error` + issue filing). **Every** dispatch route runs
  inside it — driver fetch, transcribe drain, and media download alike,
  the media stage's own first download included — so no unit's failure can
  escape the drain or be charged to another unit's line.
  The second is the canonicalization seam in `detect.canonical_url`, and it
  exists because a driver's `canonical` is arbitrary code over data an
  owner wrote into frontmatter, so what it raises is not a vocabulary any
  caller can plan for. Its two callers read a fault differently — seeding
  caught `ValueError` and let anything else abort the run, the ownership
  map caught everything and keyed the unit on the raw URL's hash — which
  is one URL holding two identities and two outcomes. The refusal is
  therefore typed once, at the one place the driver is called: whatever a
  driver raises leaves as a `ValueError` carrying the original, and each
  caller's narrow guard does its own right thing — seeding parks the bad
  seed keyed on the raw URL, which is exactly the identity the ownership
  fallback resolves to. Drivers and providers never
  broad-catch; internal raises use `raise … from e` so filed tracebacks
  keep their cause. Migrations have the one counterpart outside the
  pipeline, on the same reasoning: a migration that dies part-way leaves
  state half-moved, so the per-entry canonicalization is caught and
  skipped-with-why rather than aborting the chain (§12). The scrubber feeds the ledger `error` field; issue
  bodies carry no free text at all (the filer ruling in the issue-filer
  section).

Engine-version comparisons (error retry-on-new-engine, sync) parse to
tuples — `"0.10.0" > "0.9.1"` is False as strings, and that bug would file
issues about itself.

**LedgerEntry is a frozen, validated dataclass, not a dict**: the schema
comments above are runtime invariants enforced in `__post_init__`
(`waiting` ⇒ `needs` present; `needs` on `waiting`/`blocked` only;
`blocked` ⇒ `attempts ≥ 1`; `cap` on `skipped` only and `forced` on a
line carrying a `cap` only; `error` ⇒
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

**Bad-seed parking**: a URL that cannot even be canonicalized — a malformed
IPv6 literal, a media path climbing out of the instance root, a scheme the
transport refuses — never enters the queue as work. It parks as its own
`manual` entry with the refusal stated (`unfetchable capture URL: …`), one
per bad URL, and the rest of the item seeds and drains around it. The
containment is the point: one garbage URL in a capture must not abort the
seeding of everything beside it, and it must not disappear either, because
a silently dropped URL is a capture the owner believes was ingested. The
same pattern applies a layer in for media URLs a driver emits (§7) and for
`enrich fetch` batches (§10).

Bad seeds are **the one place a ledger key is minted from a non-canonical
URL**. Everywhere else the key is `work_hash(canonical(url))`; here
canonicalization is exactly what failed, so the key is `work_hash(url)` on
the raw string. Two consequences, both deliberate:

- `enrich mark` resolves a unit by canonical identity **first** and by the
  exact stored key **second**. A canonical-only lookup could never reach a
  bad seed — or a `job: media` line, keyed verbatim because signed query
  params ARE the resource — and the contract forbids healing either by
  hand-appending JSONL, which would leave judgment with no working tool at
  all. So pass the URL as the ledger shows it and the heal lands.
- A bad seed carries `kind: web` — the catch-all's kind — because nothing
  about an uncanonicalizable string is pattern-detectable, and the entry
  must still satisfy the schema.

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
  **The availability probe never raises**: every CLI verb runs it, so a
  model name HuggingFace rejects (`a/b/c`) costs one unavailable provider
  with the reason stated — never a crashed `status`/`run`/`transcribe`.
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
  large win on names/jargon. **Whisper keeps a prompt's LAST 223 tokens and
  discards its front** (`previous_tokens[-(448 // 2 - 1):]`), so the
  title/show is written at the END of the composed prompt and the trimmable
  vocabulary in front of it: whatever overflows the window is show notes,
  never the two names the priming exists to carry. The window is counted in
  TOKENS, which is not a character count in any script but English — 600
  characters is ~180 tokens of Cyrillic and ~690 of Chinese — so the budget
  (~200 tokens) is spent through a per-script estimate of what whisper's BPE
  will charge, measured against its tokenizer and rounded up per script
  family. An estimate, not the tokenizer itself: loading it would put a
  HuggingFace fetch in a step that primes remote providers needing no local
  model, and placing the head last is what makes the names safe, so the
  estimate only decides how much vocabulary rides along. whisper-api's
  chunk-continuity tail shares that one budget rather than adding to it, and
  trims the priming from its front for the same reason. Transcripts are
  stored **raw**, stamped
  `via`/`model` in frontmatter; corrections live downstream in digest/wiki
  where judgment already operates — enrichment stays the mechanical record.

**extract**
- `anydoc` (firecrawl-anydoc, MIT, PyPI) — default, all ten formats.
  Scanned/image-only pages → `needs: ocr`. Embedded assets come back as
  **bytes** — structurally, not as an anydoc quirk: embedded images live
  inside the container and have no URL, so `Extraction.assets = bytes` is
  part of the contract; the extract step writes them directly to
  `enrichment/<id>/` under the media caps, ledgered `job: asset`. A
  replacement extractor either populates assets (bytes, the only possible
  form) or returns none — graceful text-only degradation. Images merely
  *linked* from a document stay links in the markdown, like web body links —
  harvest judgment promotes them if they matter.
- `csv-builtin` — stdlib, zero deps. The per-field size bound is raised far
  past the stdlib's 128KB default — an embedded JSON blob is a big cell in a
  fine file — but stays finite, since one unterminated quote in a file that
  merely looked like CSV makes the whole file a single field. A reader
  refusal (past the bound, a stray newline in an unquoted field) is stated
  bad input → `manual`, never an engine bug.
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
bad-input cases (corrupt audio, a container carrying no audio stream at all,
malformed file) → the run layer maps it `manual`;
**`ProviderUnavailableError`** for call-time availability failures (API
5xx/429, missing binary, model-download failure) → the job **re-parks
`waiting`** with the reason, never burning blocked attempts — for **every**
capability, extract included (`waiting` + `needs: extract`), not transcribe
alone; an uncaught crash is an engine bug and takes the `error` path (issue
filed, retry on new engine). The availability seam is **per-format** —
`available(need, format)` — so a PDF wait never wakes for a CSV-only
provider. **Acquisition failures are not provider failures**: a failed
audio download (yt-dlp breakage, blocked enclosure GET) classifies through
the normal §5 lifecycle — `blocked` with attempts, escalating to `manual`
at 5 — never as no-clock waiting. **A 200 that cannot be the audio is an
acquisition failure too**: an empty body, a body short of its declared
`Content-Length`, or an HTML page where audio was expected (a CDN's cached
error page) is `blocked` and never written under the audio name — cached as
`<hash>.mp3` it decodes as garbage and parks the episode `manual` forever
over a fault that has nothing to do with the episode. Bytes that are merely
bad audio still reach the provider, and its `manual` stands. The blocked
line keeps
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

**A park never discards content it already fetched.** Both transcribable
kinds write what they have at park time — a podcast's show notes, a
video's description — and the drain **appends** the transcript to that
file rather than replacing it: a source that goes private during a
transcription backlog would otherwise take the fetched content with it.
One body shape per kind, whichever route produced it — the transcript is
always its own `## Transcript` section, so the drain can split a stored
body on that heading and compose onto what is already there. **The
frontmatter, not the heading, says whether a body holds a transcript at
all.** Three writers compose that body and two stamp `via`: the drain
stamps the provider's name on the transcript it appends, and the captions
route stamps `via: captions` on the transcript it fetches; neither park
stamps, so a park's body is notes end to end — a description or show notes
that name `## Transcript` themselves were otherwise truncated at the
author's own line and the truncation written back to disk. Once a body
does hold a transcript, the split takes the LAST such section, because
that is the one the drain appended.

**Capability report** (a render surface): each capability, active provider,
dormant upgrades and what they'd need —
`transcribe: whisper-local (active) · whisper-api available — set OPENAI_API_KEY`.
A note rides **any** state, active included: an ok-with-caveat availability
("model not cached — the first transcription downloads it") is exactly what
this surface exists to explain. Provider order is named once per capability
— a repeated name is refused as loudly as an unknown one, since it would
probe the provider twice and print it twice here.
Discoverable, never nagging. This is how a free-floor instance learns what a
key would buy.

## 7. Media stage

A shared pipeline stage, **not** a capability (no plausible second
implementation of an HTTP GET). **URL downloads only** — extraction's
embedded assets never pass through here (§6). Drivers return media URLs in
`Content.media`;
the stage downloads to `enrichment/<id>/media-N.ext`, honoring instance
config (`media_fetch: none | lead`), **cap 4 files, ~10MB per-file ceiling**;
every download ledgered (`job: media`, parent = owning work unit, kind =
parent's kind) — success `done`, transient failure `blocked` (normal retry
rules), oversize `skipped` with reason. Media downloads do **not** count
toward the item's 12-URL cap (that cap bounds fetched pages). The old
silent `except: pass` dies here.

**`media_fetch: none` means none on every path.** It gates the emit site —
a driver's media URLs are not ledgered at all — and the redrain alike: a
media unit already ledgered by a run under `lead` (parked `blocked`, or
`queued` from a crash window) rests exactly where it is, the way a
`waiting` unit rests under an absent provider — not fetched, no attempts
burned, no outcome written — and drains again when the owner turns media
back on. The run report notes the withheld cohort once.

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

The synced `.gitattributes` tracks enrichment binaries by name shape,
not by an extension list: `media-<n>.*` downloads and `*-asset-<n>.*`
extraction assets are LFS-tracked whatever their extension, the
markdown descriptions excepted. An extension list covered neither an
extraction asset nor a media download in an off-list format, and both
were entering git history as raw blobs; the name shape is the engine's
own naming grammar, so whatever it writes stays out.

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
- **Thread walk-up inside the driver**: the chain is
  context for the captured post, not new first-class sources. One enrichment
  file, one ledger entry — the chain is never ledgered as units of its own,
  which is why there is no `via: thread`. fxtwitter parent pointers, all authors
  included, cap-hit noted in enrichment frontmatter (below). The walk is
  **bounded at 100 hops** — a sanity bound against a chain that never ends,
  not an editorial one: the chain is one piece of content and the
  walk is linear with no fan-out, unlike the 12-URL harvest cap, which
  bounds how much one item drags in. A thread that exceeds even 100 is
  still recorded honestly rather than silently truncated.
- **Cycles end the walk where they repeat, and hops are paced.** A post
  naming itself (or an ancestor) as its parent is not a long thread: the
  ids already walked are remembered, so the repeat stops the walk at once
  and is recorded like any short chain (`chain_incomplete` + a note saying
  where it looped). Without that, one self-referencing parent spent the
  whole 100-hop bound as 100 back-to-back requests to a free community API
  for a single unit. The walk also **sleeps 1s between parent fetches**:
  the driver's 4s politeness is spent between units, and a 30-post thread
  is 30 requests inside one — a second a hop keeps an ordinary thread
  under a minute while making the walk a paced sequence, not a burst.
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
  `manual` park applies, because a post ledgered `done` on a shortlink and
  nothing else is thin garbage the digest layer cannot tell from real
  capture.
- Chain media pooled, captured post's first — **photos and videos alike**:
  both are URL downloads, and the media stage's caps apply. A media-only
  post is `done` with a minimal attributed body, whatever the media is; an
  oversize video meets the media stage's own outcome (`skipped — media
  exceeds 10MB ceiling`, charged to the media unit), never a manual park.
  "no text or media" is said only when it is true: reading photos alone
  made a video-only post park `manual` over a payload holding media, and
  dropped the video the media stage would have fetched.
- **Incomplete chains are recorded, never silently presented as complete**
  (learned from a 2026-08-20 production run — a thread's root promised a
  numbered list and not all of it existed publicly): a parent fetch failing
  mid-walk is
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
- **Direct RSS / indie episode page** → the feed the page's `<link rel>`
  names (its notes are richer than the page's markup), falling back to the
  enclosure the page carries itself when that feed is unreachable or does
  not hold the episode. Either way the unit resolves — it must, because the
  route it arrives by is a re-detection and bouncing back would park as a
  loop.

  **The route is content-driven, never URL-guessing.** `matches()` stays
  narrow (Apple, Spotify, explicit `.rss`) because bare `/feed` and `/rss`
  suffixes are blog vocabulary and an RSS `<link rel>` in a head says only
  "this site has a feed". Registry order does the work instead: nothing
  claims an indie episode page, so the catch-all fetches it, and a page
  whose **audio is its subject** re-detects to `podcast` (§1's mid-fetch
  discovery, `web → podcast`). Two markups say that, and only two: an
  `og:audio` pointer — the publisher naming the audio as the page's own
  object — or an `<audio>` element **on a page with no substantial body**,
  where the player is all there is. An `<audio>` element beside a real
  article is not the signal: mainstream publishers ship read-aloud
  text-to-speech widgets and encyclopedias embed media samples, and the
  catch-all rule handed those articles to `podcast`, which resolved the
  widget as the enclosure and parked `waiting: transcribe` with an **empty
  body** — the article never extracted, its links never harvested. An
  uncommon shape, and one a real corpus does contain. The asymmetry
  decides every tie: a false positive
  costs the whole article, a false negative costs a podcast page keeping
  its show notes instead of a transcript. A bare link to an mp3 is not the
  signal either: a post linking one is still a post. The signal itself is
  `drivers/audio.py`, a shared seam beside `transport.py` and `gh.py` —
  both drivers read it (web to route, podcast to resolve) and neither
  imports the other.

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
promoted, at any depth. **The judgment is the whole mechanism.** No driver
reports links to promote and none ever did: the subject rule is a reading
of what the item is about, which is not a question a fetcher can answer, so
a session reads the enrichment and names the URLs. The engine bounds what
it is handed: promoted children re-enter with `{parent, via: harvest,
depth}`, caps depth 4 / 12 URLs per item.

Engine primitive: `dex enrich fetch <item-id> <url>…` — fetch specific extra
URLs into an existing item, ledgered as child entries. It is the ONE road
into the queue besides a capture. Semantics: `parent`
defaults to the item's primary work unit (override with `--parent <hash>`),
`depth` = parent's depth + 1, and both caps bind the admission — fetches
count against the item's 12-URL cap, and a fetch past depth 4 is refused
too. `--force` may exceed the URL cap for an owner-requested deepen (the
cap fire is still recorded); it does **not** reach past depth, which is a
hard bound, so a depth refusal offers no route it does not have.
**One bad URL never aborts the batch**: an
uncanonicalizable URL parks as a bad seed, a URL already enriching under
another item is reported (one URL enriches under one item — the batch
continues without it), and a capped refusal is reported naming the bound
and whatever route it has — the session asked, so every refusal comes back
as an answer on the report, and the rest of the batch still fetches. This
is also how Claude deepens any item on
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

**The pass record has a reader on both sides of its distinction.** The
health check flags harvest passes recorded under an older rules version
(re-judge), and it flags harvest that **never ran**: a live item that owes
no further work, has at least one fetched page on record — a `done` unit
that is not a media download or an extracted asset, the URL cap's own
reading of "page" — and has no harvest pass under any rules version.
Without that reader, a session that skipped harvest left an item digested
and cited on the strength of the shared link alone, on no surface. The
predicate follows the ingest procedure's own bounds: an item still owing a
unit derives `raw` and has not reached the harvest step, so it is never
listed; a no-source capture, and an item whose every unit died unfetched,
has no page to read the subject rule over and owes no pass — its owed work
is description and digest, written from the owner's note. The run report
names such an item, the `enrich status`/lint digest backstop lists it
until its digest pass is recorded, and recording that pass is what clears
it. A recorded
pass covers its item by trailing shortid, the same match exclusions use
across renames, so a renamed item's standing record never reads as a
skipped judgment; an item re-seeded by migration keeps its original pass
and answers to the rules-version check, not this one. The finding fires
beside the enrichment-newer-than-digest backstop rather than deferring to
it — the backstop's repair is digest → place → wiki, which never runs
harvest, so deferring would carry the skipped judgment straight through
the repair. Like the stale-pass row it is a finding, never exit 1: no
later mechanical stage breaks on a missing harvest pass the way the wiki
layer breaks on a malformed digest, and the repair — run the judgment now,
then `enrich pass --stage harvest` — is judgment, the report's business.

**The digest pass is recorded by the digest verb, not by a second
command.** `enrich item digest` records the pass itself, through the same
`record_pass` path `enrich pass` uses — a separate recording command was a
step a session could forget, and a forgotten one silently cost the
staleness backstop its comparand: a digest with no pass record is dated by
nothing, so no later enrichment could ever read as newer than it. The
verb records after validation and before the file write, so both failure
shapes stay honest: a refused payload records nothing, and a crash
between the record and the write leaves a pass with no digest file —
which the backstop's no-digest branch lists loudly — never the unreadable
opposite. `enrich pass --stage digest` remains the manual re-record, and
the harvest and wiki stages still record through `enrich pass`.

## 11. Rendering: judgment decides, code renders

Composition that is fully determined by data is computed in code and emitted
verbatim — never re-derived character-by-character by the model.

```
render/kernel.py     markdown composition, zero dex vocabulary — headings,
                     bullets, detail lines, `·`-joined inline lists, and the
                     pluralizer that makes a heading's scale read as
                     English. No width, no wrapping, no truncation:
                     identity reaches the reader whole. Composition bugs
                     can exist in exactly one place, and it has tests.
render/surfaces.py   named surfaces, loud payload validation:
                     enrich-report, status, item-status,
                     capability-report, sync-report, ingest-receipt,
                     health-report
```

Two call paths: engine-internal (e.g. `enrich run` renders its own report
in-process — includes a "reported upstream: N issues" line when the filer
acted) and cognitive (Claude writes a JSON payload — including free-prose
fields like `notes` — to `cache/`, runs `bin/dex render --file …`,
emits the result verbatim; even prose position/framing is deterministic).
Standing skill rule: **never hand-draw a report; there is a surface for it
— call it.** A cap refusal is answered on the report of the run that asked
for it, and every cap fire nobody waived is read on the health check as a
tuning signal — two surfaces, two jobs, both done (§1).

### Who reads a report

The primary reader is the Claude session driving a scheduled run. It reads
the report to decide what to do next, and it reads every line of it. The
owner reads the same reports rarely, in whatever client he happens to be in.

The surfaces were originally laid out for a person at a 72-column terminal:
fixed-width tables, column fitting to the widest cell, greedy wrapping, and
hanging-indent continuation. Nothing was ever elided or truncated — the old
kernel had no elision and no display-column measurement in it — but a token
wider than the budget was hard-split across lines mid-string. A URL of
several hundred characters arrived in the reader as several fragments, and
an item id could break
across a line boundary the same way. That costs the two things these reports
exist for: an id a grep can find, and a URL that survives a copy. It also
bought nothing, because the primary reader is a Claude session in a client
with no fixed width at all, and the owner reads the same reports in whatever
client he happens to be in. So **every surface emits markdown, not
fixed-width layout**: no column arithmetic, no padding, no rules, no box
glyphs, and identity is never split.

### Layout rules

**Headings.** `##` opens the report and names it plus its scale
(`## Enrich run — 6 units processed`). `###` opens each section and names
the section plus its scale (`### Needs you — 1 entry only you can move`). A reader who stops at the headings still knows what happened and how
much of it there is. **No surface is exempt**: the two that carried bare
headings — the capability report's `## Capabilities` and its per-capability
`###`, and the health report's `### Wiki` / `### State` / `### Digests` —
state capabilities, providers, pages, ledger entries and digests
respectively, every one of which the site already had in hand. The health
report's `State` is the case worth stating: a ledger that will not load has
no scale, so that heading says `ledger unreadable` rather than `0 ledger
entries`, which would report an unreadable file as an empty one. Where a
section genuinely has no number to give, that is the heading's problem to
say out loud, not a licence to drop the scale.

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
are long and real URLs longer still — several hundred characters is an
ordinary length for one. Nothing in
the render path may shorten one, split one, or elide the middle of one. The
code that could split one is deleted rather than bounded, because a bound is
a setting and a deletion is a guarantee — and with it went the rest of the
terminal geometry the kernel held: greedy word wrap, hanging-indent wrap,
fill-to-width, table column fitting, key-value blocks and tree gutters.

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
the reader must do about it. One exception rides the row, not the status:
a parked media unit under `media_fetch: none` is deferred by the drain,
never retried, so it waits on the owner flipping the config — the payload
builder, the one place the config is in hand (payloads stay
self-contained), marks the row `resting`, and it renders under **Needs
you** with a reason naming what unblocks it and no retry framing.

**Settled — where `error` entries belong** (owner ruling, 2026-08-24):
under **Waiting on the engine**, where they already sit. The split's rule
is who owns the next action, and an error's next action is the engine's:
the issue is already filed (§13) and the unit retries itself on the next
release (§5) — the reader has nothing to do but wait. A **Needs you** row
with no procedure attached would be noise, and the counter-argument (the
retry needs a release to happen at all) describes how long the engine
takes, not who acts next.

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

### Reader-facing vocabulary

| label | what it holds |
|---|---|
| **Needs writing up** | items with new enrichment for the session to digest |
| **Read these yourself** | jobs resolving to the cognitive floor (OCR, extraction Claude must do with eyes) |
| **Needs you** | entries the engine has given up on (ledger status `manual`), plus media units resting under `media_fetch: none` — the owner's config change is what moves them |
| **Waiting on the engine** | entries the engine retries unasked: `blocked`, `waiting`, `error` — minus media units resting under `media_fetch: none`, which it will not retry |
| **Not finished** | items still `raw` because a unit they own has not landed |
| **Digest these** | items whose enrichment is newer than their digest |
| **Waiting on a capability** | the `waiting` cohort, counted by the capability it needs |
| **Repair with judgment** | a migration's skipped records |
| **REVIEW REQUIRED** | a migration's anomalies |

(That table is documentation, not a rendered surface — the no-tables rule
binds the renderers, not this document.)

Detail text says what changed in plain terms. "3 new enrichment files, 1
rewritten" replaces "3 new, 1 changed", which never said new *what*.

## 12. Releases, sync, migrations

**Tag-pinned, auto-upgrading.** The `bin/dex` shim reads a pinned tag from
**`.dex-engine-pin`** (one line, instance root, committed, instance-owned —
sync writes its *value*, it is not a synced-template file) and runs
`uvx --from git+…@<tag>`. Bootstrap: the first tag-aware sync creates the
file pinned to that sync's own release. Mint
cuts releases and changelogs — human-invoked, never the engine; the engine
only consumes releases. A broken push to main reaches nobody until tagged;
rollback is editing the pin line. **The tag is gated**: `.mint.toml`'s
`preflight` runs the four gates before Mint does anything else, and a
failure aborts with no bookkeeping commit and no tag (§15). The live suite is
excluded — a third party having a bad afternoon must not block a release.

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

**Sync deletes retired engine skills.** The `dex-` namespace under
`.claude/skills/` is engine-owned, so a `dex-*` directory the template no
longer ships is removed from the instance, and the removal is reported as a
machinery change. Copying alone is not enough: a retired skill left on disk
keeps loading its stale procedure into every session, describing verbs that
no longer exist — which is worse than having no skill, because the session
believes it. (`dex-ingest`, split into `dex-capture` + `dex-run`, is the
case this exists for.) The same holds INSIDE a live skill: a synced `dex-*`
directory mirrors the template exactly, so a reference file the template
dropped is removed too, reported the same way — copy-only sync left it
loading its stale procedure in every instance forever. The mirror binds
shape as well as content: where a release replaces a synced file with a
same-named directory, or folds a directory down to a file, the conflicting
entry is removed — reported as a machinery change — and the template's
shape is written; copy-only sync crashed on the standing entry instead and
took the whole sync with it. A symlinked skill
directory is unlinked rather than recursed into, so whatever it pointed at
is left alone. Nothing outside the `dex-` prefix is ever touched — an
owner's own skills are instance-owned.

Majors also auto-apply (the always-migratable commitment) but announce
loudly twice: in session before the re-exec (the echo of the process about
to replace itself), and distinctly on the post-re-exec sync report — the
heading names the MAJOR upgrade and a loud line leads the report, the
transition read off `--previous-pin`. A major is an owner-visible event,
never a row a patch bump would also render. Minors only for the
foreseeable future.
Transition bootstrap is automatic: sync rewrites its own shim, so existing
instances move from main-tracking to tag-pinning on their next sync.

**Version precondition on every release**: the package version stays ahead
of every released tag. A running version equal to an existing release makes
the pin bootstrap adopt that (old) tag and brick the instance on older code.
The pre-rewrite marker `0.0.1` is never re-tagged or re-stamped.

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
- **Untranslatable ledger lines are DROPPED, never left in place and never
  parked for a human** (owner ruling): a migration is fully automated and
  leaves no residue. A line a migration cannot translate is first attributed
  as hard as the state allows — its own `item`, the enrichment tree on disk
  (outputs are `enrichment/<item>/<kind>-<hash[:6]>.md`, so the file a unit
  produced names its owner), its recorded output path, then the corpus URLs
  — and only what survives all four is dropped, with a count and a short
  capped list in the report naming what went and why. **The tree is asked
  before the recorded path** (amended at phase-4 review, on real state): an
  item RENAMED since the line was written leaves that path naming a
  directory that is gone while the output sits under the new id, so trusting
  the string attributes a live item's finished work to an id no corpus file
  answers to. Where the tree answers and the recorded path
  disagrees, the path moves onto the attributed item too — but only where
  the file is demonstrably there, never as a guess.
  **Attribution resolves to a LIVE corpus item or it does not resolve**
  (owner ruling at phase-4 review, on real state): if an item is dead, its
  stuff goes; nobody gets clever rescuing it. An excluded item loses its
  corpus file, its `enrichment/<id>/` directory and its digest, but a done line's
  recorded `path` still spells the id — and every derived attribution is
  therefore checked against `corpus/<id[:4]>/<id>.md` before it is accepted.
  An id with no corpus file is skipped over, the next attribution is tried,
  and a line left with none is dropped and named like any other. Rescue
  effort is reserved for items that are still there: a renamed item — same
  shortid, new slug — resolves through the enrichment tree or the corpus
  URLs and keeps its work, path repoint included, because that is a live
  item's transcription. The corpus URL map needs no check, being built from
  the corpus files themselves. Nothing will ever seed such a line again, so
  this is the only place the residue can be dealt with. That is safe
  because of what the ledger IS: the corpus is the source of truth and the
  ledger is derived work state, so seeding re-raises anything that still
  matters through the front door on the next run; and the pre-migration
  ledger is committed in git history, so nothing is ever truly destroyed.
  The main ledger must load clean after every migration — a line that
  poisons `ledger.load` also bricks `enrich mark`, the sanctioned repair
  verb, leaving judgment with no working tool. And parking these lines for
  a human was never a real repair: `enrich mark` heals a unit, and a line
  with no attributable item is exactly the unit it cannot create.
  **A malformed tie-breaker is never grounds for dropping a line**: where a
  migration cannot read `at`, it drops the value, names it in the report,
  and keeps the line — which then orders by file position, exactly as every
  line did before the field existed. Load-bearing fields (`item`, `kind`,
  `status`, `date`) keep their strictness, and `ledger.load` keeps its own:
  a malformed field in a live ledger still refuses to load. This applies
  only to a migration's disposal path, where the cost of strictness is a
  deleted work history rather than a loud error.

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
   used `error` as an informal reason field on non-error statuses (wild
   lines across dead/nocaptions/done/skipped/manual, plus a `note` field) —
   migration 1 **never destroys those values**: on a status that may carry
   a reason they move into `reason`, on `error` they stay in `error`, and
   on `done`/`queued` — where §5 forbids `reason` outright — the text is
   preserved in the migration report and dropped from the line, which the
   report says in as many words. A `manual`/`skipped` line the old engine
   left with no reason at all gets one, since the schema requires it.
   Must precede any requeue (else reruns write `x-….md` beside stale
   `tweet-….md`).

   No guard on an absent or empty corpus — settled. Migration 1 reads the
   corpus to attribute ledger lines, and it runs without checking the
   corpus is populated first: corpus and ledger live in the same repo and
   arrive together, so an empty corpus beside a populated ledger is not a
   state a checkout produces — and a guard would teach a migration to
   distrust its own repo, which is the ghost-item sweep's mistake in a
   different costume. The dress rehearsal drives the real states.
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
   computes: `job: media` (media URLs are fetched verbatim, signed params
   included), `job: asset` (the key is a repo path, which
   canonicalization would mangle into `https:enrichment/…`), and any entry
   whose URL is not an absolute http(s) URL. Canonicalization is guarded
   per entry, as in migration 1 — one unreadable URL is skipped-with-why
   and keeps its stored hash, never aborting the chain part-way through.
   The rewrite is atomic and idempotent — a second apply finds every
   identity already current.
   **The unit's output file moves with the hash** — migration 1's hazard,
   one half of the name along: an output is named `<kind>-<hash6>.md`, so
   a re-key touching only the ledger leaves the file under an identity
   nothing computes any more, the rerun seeded below lands beside it (two
   views of one unit, both listed in engine-owned `enrichment:`
   frontmatter), and a re-keyed youtube park loses the description the
   transcribe drain reads back out of that file. So the file is renamed
   under the new hash and the entry's `path` repointed to it. Every kind
   prefix is tried, since a unit redetected since its line was written has
   its output under the older kind, and each candidate proves it belongs to
   this unit by the `url:` it records — six hex digits collide, and moving
   a neighbour's file would strand THAT unit. A target that already exists
   — both old spellings of one post landing on one identity — is refused
   and reported, never clobbered.
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
   only page work: a `job` unit (a media download, an asset write) is a
   byproduct of its parent's fetch with no links to keep and no thread to
   walk — seeding one would strip its routing and lineage and send a
   signed media URL or an asset repo path to the web driver, and the
   parent page's own rerun regenerates its media children anyway. And
   only entries whose work a **live corpus item still claims**:
   `dex exclude` deletes the item, its enrichment, its digest **and the
   ledger entries
   for work no surviving item claims**, so a seed keyed to a purged item
   would re-fetch content ruled out of scope and put an owner ruling back in
   the queue. (Purges made before `exclude` swept the ledger left their
   entries on file — but migration 1 runs first and drops a line it can
   attribute only to a dead item, so by the time this migration reads the
   ledger every entry names a live item.)
   **`exclude` purges work units, not item names** (amended at phase-4
   review, on real state): a unit is keyed by URL, so two corpus items
   listing one URL share one entry that names only one of them, and a hash
   claimed by more than one live item is common in a real instance. The
   hash is
   judged on the line `load` resolves to, and a hash any surviving corpus
   item still claims is kept whole; the corpus is scanned after the
   deletions, so a purged item cannot claim its own work. A kept landing
   whose output lived in the deleted `enrichment/<purged>/` goes back to
   `queued` in the same command, named after the item that claims it: the
   survivor's URL would otherwise be `done` with nothing on disk, and
   seeding's already-a-unit short-circuit means nothing would ever fetch
   it again — the item owes nothing, derives `enriched`, and the digest
   and query layers have no file to read. It is recorded here rather than
   inferred by a later run because here the deletion is a fact, and a
   recorded path is missing for innocent reasons too (a rename moves the
   enrichment directory as well, and re-fetching on that would put a
   `job: asset` unit's repo path into the fetch queue, where no
   transport can take it). The summary line states the counts — dropped,
   kept, and any re-queued — because `exclude` runs in bulk from the
   scope-filter pass and that line is the owner's only signal. One
   id twice in a batch is not a refusal, for the reason a re-run is not:
   excluding an item is idempotent by construction, so the second copy asks
   for what the first already did. The copies collapse to one and the
   summary says how many — `excluded N (M duplicate id(s) collapsed)` —
   because applied twice they wrote the permanent `state/exclusions.tsv`
   record twice and reported the second pass as an item already gone, on
   that same only signal.
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

   Lint reads the same three cases and asks the `state/exclusions.tsv`
   record **before** the claim. A hash two live items shared always has
   another claimant once one of them is excluded, so asking the claim first
   reported every deliberate on-the-record exclusion as a rename — asserting
   a cause the check never established over the one the owner wrote down.
   The claim still shows (`excluded on record — <id> shares this work`), as
   the reason those lines survived the purge rather than as what became of
   the item.
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

That is the whole shipping set: 1, 2, 3.

**A ghost-item sweep was drafted as migration 4 and deleted** (owner ruling
at phase-4 review): permanent machinery, shipped to every instance forever,
to tidy a handful of lines in one instance. Its predicate — infer "purged"
from "the corpus file is missing" — is ambiguous by construction: a rename,
a partial checkout and a hand deletion look identical to it, and every guard
bolted on afterwards was a patch over that first guess. It cost a real
transcription before it was caught. The residue it existed to
clean is created by migration 1 attributing a line to a dead item, so the
fix belongs there and nowhere else: migration 1 attributes to a **live**
corpus item or drops the line, per the attribution rule above.

**No new migration without explicit approval** — the same ruling. A
migration is code every instance carries forever; a one-off tidy is a
one-off tidy.

Cap-event surfacing, blessed precisely: an `enrich fetch` refusal IS
surfaced on that run's report (whoever asked deserves the answer), and the
same fire is read on the health check as a tuning signal unless `--force`
waived it (§1); media-cap and oversize skips
surface as ordinary unit outcomes per the media-stage rules.

Resurrected transcription backlogs are bounded: the transcribe drain takes a
per-run cap (default 10) so a first sync never monopolizes a machine.

## 13. Issue filer: decentralized self-healing

Instances auto-file engine bugs at the public engine repo; the owner's Claude
session fixes; Mint releases; every instance heals at next sync. Human
involvement: the engine owner only. One mechanism, two producers: the
exception path below, and the session-observed verb at the end of this
section.

- **The exception producer fires only on `status: error`** (deterministic
  engine exceptions).
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
  the owner's visible record of what their instance reported. Every
  report writes its record whether or not the gate let it file upstream:
  the record carries `filed: true|false`, and dedup and refile
  suppression treat any record as seen, so a defect observed under a
  closed gate is never auto-refiled when the gate opens later.
- **Rate limit**: max 3 new issues per run.
- **Fire-and-forget**: instances never poll issues or act on tracker
  content — the fix loop closes through releases and sync only. The public
  repo cannot instruct instances.
- **`gh` is assumed** — it's a declared instance dependency; an environment
  without it is not a dex environment (the Cowork VM class was discarded for
  exactly this kind of lack). No pending-file/retry machinery. The filer's
  only soft edge: its own failures are wrapped and non-fatal — the report
  notes "issue filing failed" and the run continues.
- Config: `report_issues: true` (default) in `state/config.json` — a gate
  on upstream filing only, never on the local record. Filings appear in
  the run report, so the human always knows.

### The second producer: session-observed reports (`bin/dex issue`)

The filer above fires on exceptions. A session can also SEE a defect
nothing raised — a report contradicting state on disk, a documented
behaviour that did not happen, a verb writing the wrong thing quietly —
and on a scheduled run that observation reaches nobody unless it is
filed. `bin/dex issue --file <payload.json>` (its own `dex-issue` entry
point, since the shim dispatches `bin/dex <cmd>` to `dex-<cmd>`) is the
observation-shaped producer over the SAME mechanism: both producers
reduce to one `Filable` shape and go through one filing pass
(`file_reports`), sharing `ENGINE_REPO`, the `gh` seam, fingerprint
dedup against `state/issue-reports.jsonl`, the open→comment /
closed→regression arithmetic, the rate limit, the `report_issues` gate
and the note-never-crash edge.

- **Structured payload, nothing free-form filed**: `verb` — the
  misbehaving command, validated against the actual CLI vocabulary (a
  maintained constant, pinned by test to pyproject's entry points and
  enrich's argparse tree); `expected` and `observed` — one mechanics
  sentence each, ≤ 90 chars; optional `steps` — ≤ 8 statements, ≤ 120
  chars each. Title:
  `[observed] <verb>: expected <clause>; observed <clause> fp-<hash>`
  (the clause bounds keep it under GitHub's 256-char title cap); body =
  the validated fields + engine version + date + fingerprint, the same
  enumerated-allowlist discipline as the exception body. Fingerprint =
  `sha1("observed|" + verb|expected|observed)[:12]` — steps and note
  excluded (repro detail churns without the defect changing), the
  domain prefix disjoint from exception fingerprints.
- **Privacy by REJECTION, never scrubbing** (owner ruling): every
  public field runs the scrubber's own detectors — URL, email, home
  path, the strict item-id grammar, instance-content tokens; one
  spelling, shared via `classify.py` — and any hit refuses the whole
  payload with per-field reasons naming what to abstract. Nothing is
  filed, nothing written, exit nonzero. Where the exception path
  eliminates free text structurally, this path admits two
  session-written clauses only because a mechanical detector stands
  between them and the public repo — and the detector's answer to a
  leak is no, never a redaction with residual risk.
- **The local note split**: the payload may carry `note` — free text,
  ≤ 2000 chars, exempt from the detectors BECAUSE it is never filed. It
  lands only in this instance's `issue-reports.jsonl` record
  (`{fingerprint, action, engine, date, issue?, note?}`), where the
  owner reads it and forwards what matters by hand.
- **Skills**: the rubric lives where a session is when it would notice
  a defect — a guardrail in dex-run's SKILL.md, the full rubric in its
  processing reference (file for engine misbehaviour only, never for
  world/content failures; mechanics, never content; filing is a side
  errand, never a stop), the payload shapes in the state-formats
  reference, and dex-lint's repairs reference pointing at the same
  rubric.

## 14. Module layout and dependencies

```
src/dex_engine/
  pipeline/    types.py  ledger.py  detect.py  registry.py  run.py
               classify.py   the ONE failure classifier (§5) + the scrubber
               transcribe.py the transcribe drain: audio acquisition,
                             prompt budgeting, chunking (§6, §9)
               enrichment.py the ONE enrichment-file format point: the
                             renderer and its two readers, inverses over
                             frontmatter.py's scalar unquoting rule, plus
                             the transcript-body section contract the
                             youtube driver and the drain share
               urls.py       canonicalization and the work-key hash — the
                             one place a ledger identity is computed
               capture.py    `enrich item new`: capture file → corpus item
               digest.py     `enrich item digest`: JSON judgment → the
                             digest file. The ONE digest write point; the
                             payload carries `signal`, `topics`, optional
                             `entities` and the facts, and `date`/`media:`
                             are read off the corpus item rather than
                             taken from a caller
               issues.py     the issue filer (§13): the shared filing
                             mechanism (Filable → file_reports) plus the
                             exception-shaped producer
               observed.py   `dex issue`: the observation-shaped producer
                             (§13) — payload validation and the leak
                             rejectors (refusal, never redaction) over
                             classify's own detectors
               ownership.py — which live corpus item claims a work unit
                 (its urls:/media: hashed exactly as seeding does), the one
                 answer to "is this entry's item still there?"; lint and
                 migration 2 share it so a renamed item cannot read as a
                 purge in one place and a rename in the other; `exclude`
                 asks it before deleting work history, for the same reason;
                 and the drain asks it as it WRITES — the outcome line and
                 the enrichment path both — so a stale stored `item` never
                 needs healing (§4)
  drivers/     youtube.py  x.py  github.py  paper.py  podcast.py  web.py  file.py
               transport.py  the HTTP seam (§5's OSError normalization)
               fetch.py  gh.py  audio.py  ytdlp.py  article.py — the
                 shared lib layer beside the drivers (§2): the
                 fetch-and-classify pairing, the authenticated GitHub
                 route, the audio-on-a-page signal, the yt-dlp seam
                 (probe, audio download, failure vocabulary, audio-cache
                 scan) the youtube driver and the transcribe drain both
                 consume, and the article-fetch route (extraction,
                 thin-extraction parking, wayback rescue, mid-fetch
                 re-detection) the web and paper drivers both consume
  capabilities/
    transcribe/  whisper_local.py  whisper_api.py
    extract/     anydoc.py  csv_builtin.py  cognitive.py
    ocr/         cognitive.py
  atomic.py    the ONE atomic-write implementation (same-dir temp file,
               then one replace; no temp orphan on failure) — every state
               write shares it: ledger, corpus items, enrichment outputs,
               the pin file, capture pointers, cached audio, migration
               rewrites
  frontmatter.py
               the ONE frontmatter-scalar unquoting rule (double-quoted
               read as JSON, single-quoted as YAML's literal form), shared
               by the enrichment reader and lint's digest check; imports
               nothing from the package
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
               · item digest --file <payload>  (writes the digest: the
                 session's judgment arrives as JSON, the fence, field
                 order, list style and bullets are the engine's. Loud
                 total validation — a missing, unknown or mistyped key, a
                 `signal` outside the vocabulary, empty `topics` or
                 `facts`, an id naming no corpus item — and nothing is
                 written on a refusal, no pass record included. Records
                 the digest pass itself, before the file write (§10).
                 Rewriting is allowed and carries
                 nothing over: every field is the payload's judgment or
                 the corpus item's fact)
  issue.py     thin CLI: `bin/dex issue --file <payload>` — files one
                 session-observed engine defect through the shared filer
                 (§13); a payload naming instance content is refused with
                 per-field reasons, nothing filed and nothing written
  normalize.py imports shared detect/types (private kind_of copy deleted)
  inbox.py     materialized files feed the pipeline (format detect → extract)
  lint.py      grows checks: ledger schema, ledger↔tree referential
                 integrity (items with no corpus file — excluded-on-record
                 told apart from renamed, whose URLs a live item still
                 lists, and from unclaimed; one row per item and finding
                 with its entry count; done outputs missing on disk, and
                 done outputs filed under another item's directory — both
                 asked of the owning item, §4),
                 waiting cohorts, pass records, the judgment-drift signals
                 nothing else reads (cap fires off the ledger, thread
                 markers off enrichment frontmatter), and digest shape —
                 the backstop for digests written before `item digest`,
                 and for anything hand-edited since. A broken state
                 file renders as a failure row naming the offending
                 file (a malformed `entity-members.json`, a torn
                 `passes.jsonl` line — named by file and line, since
                 later appends or a union merge can leave the tear
                 mid-file) rather than dying bare, so the report
                 survives to name the repair. Exit 1 is the
                 hard-failure set: broken wikilinks, bad citations, a
                 ledger schema error, a malformed taxonomy, a malformed
                 `entity-members.json`, an unparseable pass record, a
                 malformed digest, and the pre-taxonomy
                 broken-mid-ingest state (corpus items on disk with no
                 `state/taxonomy.json`, placement never having run);
                 everything else is a finding for the report
  sync.py      grows: pin resolution, re-exec, migration runner, sync
                 report. A torn `migrations.jsonl` line fails with an
                 error naming the file, the line, and the sanctioned
                 repair: delete the named torn line — a half-written
                 line is not a record, so removing it is healing, not
                 hand-writing state, and intact records are never edited
```

Dependencies: `trafilatura` (extraction only), `yt-dlp`, **`firecrawl-anydoc`**
(+), **`faster-whisper`** (+, packaging pending — §17), `pypdf` (−, replaced
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
- **Progressive disclosure governs the two operating skills whole.**
  `dex-run`'s SKILL.md is the always-in-context skeleton — mission,
  numbered steps with their load-bearing invariants inline, a "read
  `references/<file>.md` now" pointer per phase — over
  `references/preparation.md` (anchor → sync → guard → pull → inbox),
  `processing.md` (items → enrich → backstop, dispatching to
  `ingest-item.md`) and `backfills.md`, alongside the schema/state-format
  references. `dex-lint`'s skeleton loads `references/repairs.md` and
  `standing-signals.md`. The preparation steps live once, in dex-run's
  `references/preparation.md`: run standalone, dex-lint reads that file,
  makes the instance current, and stops short of processing — no items
  created, no enrich run — never a second copy of the steps; dex-run
  remains canonical.
- **Skill authoring style**: deterministic behavior lives in code, never
  prose; order-critical sequences stay prescribed *with their reasons
  attached* (the "commit immediately — the repo copy is now the only copy"
  pattern) so deviations protect the invariant, not the letter; judgment
  work is described as goal + quality bar + boundaries (anti-improvisation
  rules stay), not scripted with gates. Written for whatever model an
  owner's scheduled task runs — the safety comes from the layers below
  (code constrains, lint verifies), not from prose density. And skills are
  written for progressive disclosure: a lean orchestrating SKILL.md — the
  step sequence with one-line summaries, invariants that prevent
  catastrophe stated inline — with each phase's detail in a reference file
  read at the step that names it. Instructions loaded before they apply
  must be ignored-for-now, and that is an ambiguity surface; reveal
  instructions when they become actionable. Split at phase boundaries
  (where the session's work changes mode), one reference per phase, never
  one per step — each read is a context load.
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
  frontmatter, Claude writes prose. The governing line: judgment supplies
  the values, code writes the shape. It holds even where every value is
  judgment — **digests go through `item digest` too**: `signal`, `topics`,
  `entities` and the facts are the session's, and serializing them is not
  a decision the session should be able to get wrong. Frontmatter thereby
  converges on the same guarantee as the ledger: a malformed item or
  digest can't be written, because structure never passes through freehand
  writing, and lint's digest check becomes the backstop for files that
  predate the verb rather than the primary guard. Corollary, closing a
  drift current practice shows: **the item
  body is the owner's note verbatim and stays that way** — Claude's
  interpretive context (what a linked video is, how a thread relates)
  belongs in the digest, not the item body; thread context itself lives in
  the enrichment via walk-up. After creation, exactly two frontmatter
  fields ever change (`status`, `enrichment:` listing), both derived, both
  written by corpus.py. The derivation rule: the listing is the markdown
  filenames in `enrichment/<id>/`, sorted; `status: enriched` iff **no unit
  the ownership map gives the item is still outstanding** — queued,
  waiting, blocked, error, or manual. An item is one unit of knowledge (the
  post, the thread parents above it, the links harvest promoted, the media,
  the video awaiting its transcript) and nothing about it advances to
  digest or wiki until every part has landed, so one pending transcript
  holds the whole item at `raw`; a `dead` or `skipped` unit owes nothing and
  holds nothing hostage. What the item OWES is the whole question and the
  ledger answers it, never the item's own directory listing — which asks a
  different question, where the item's own files are. The one exception is
  the capture that seeded nothing at all: no units AND no files is a
  text-or-image-only share, which owes its description and its digest, and
  an `any()` over no units would call that finished. So an `enriched` item
  may legitimately carry `enrichment: []` — seeding dedupes by work hash,
  so a URL shared into two captures is ledgered once and its output lands
  under the first item only, while the second derives `enriched` on the
  same landing. The listing stays empty for it deliberately: the field is
  the item's own directory, its entries are bare filenames every reader
  resolves against `enrichment/<id>/`, and one file for one unit is named
  by one item. `enrich status <item>` is where the shared unit shows.
  Every run reconciles **every** item against
  disk — not just the units it drained — writing only on change, so
  enrichment written outside the drain (media descriptions, cognitive
  heals) converges at the next run; `enrich mark` and `enrich pass`
  refresh their owning item in the same call, so frontmatter is already
  true when the session that wrote the file ends.
- **Wiki frontmatter's derived fields are lint-repaired, not carefully
  authored**: `lint --write` reconciles `items:` counts and `generated:`
  dates mechanically at health checks (the field the 2026-08-20 analysis
  found drifting across the wiki). Wiki *bodies* remain the most freehand
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
  migration rewrites every one of those items again for ordering alone,
  burying the migration's own commit in noise. Per the repo's anti-drift rules:
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
  convention and the drift the 2026-08-20 analysis found are both
  member-semantics — pages were silently drifting when it looked, caught by
  accident rather than mechanism), and a **shortid-shaped
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
- **Scheduling setup lives in the install guide, not the run skill.**
  dex-run is schedule-blind — purely the Every-run procedure. A session
  running it cannot see whether a desktop task exists; it may be driven by
  a terminal, a scheduler, or a non-Claude agent, none of which can arm a
  desktop task; and a run that opens on a setup question cannot also work
  fully unattended. `docs/start.md` Step 5 carries the task mechanics —
  name, exact prompt (the load-bearing folder line included), model
  recommendation, Folder-field fix, the one attended Run now, local-task
  semantics, and the no-desktop-app path.
- **Model recommendation**: skills can't pin a model — scheduled tasks can.
  The install guide's scheduling step recommends the owner set the task's
  model to **Opus-class or above**. The system isn't Claude-exclusive (any
  agent runtime that reads skills could drive it) but is untested
  elsewhere; the guide says so rather than pretending portability is
  verified.

## 15. Testing

The Protocol/pipeline split exists partly to make the system testable; the
old monolith had no seams.

- **Hermetic driver tests** against `tests/fixtures/` (fxtwitter JSON, GitHub
  payloads, VTT, tiny real docx/pdf/csv), asserting what the fetch FOUND —
  the §2 outcomes — while the run-layer tests assert the outcome→status
  mapping, one pin per match arm. Regression pin on the motivating
  incident: a 403 fixture must come back a transient `Refused`, never
  `Missing`.
- **Pipeline tests with fake drivers**: seeds/children/reruns born `queued`
  and drained, promotion + provenance through `enrich fetch`, both caps
  fire and record over a real chain, waiting ignores runs
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
- **Render kernel tests**: the markdown composition primitives (§11).
- **Shim tests across shells**: `instance/dex` is POSIX sh, and "POSIX sh"
  differs by platform — macOS's `/bin/sh` is bash in POSIX mode and forgives
  bashisms a Linux instance's dash kills the script over. The shim cases are
  parametrized over `sh`, `bash`, `dash` and `ksh`; CI names the shells the
  Linux runner guarantees in `DEX_SHIM_SHELLS`, so a missing interpreter
  fails rather than skips.
- **Live tests opt-in** (pytest marker) for real-API drift checks. CI's gating
  run is hermetic only (enforced by the default `-m "not live"`, §14).

**Gates and CI.** The suite, `ruff check`, `ty check` and
`ruff format --check` are the four gates. They run on every push and pull
request (Linux × Python 3.11/3.12/3.13, plus one clean macOS box on 3.13;
lint, types and formatting once, since all are pinned to py311 semantics), and
again as `.mint.toml`'s release `preflight` so a tag cannot exist over a red
tree — instances pin tags, and rollback is hand-editing pins. The format gate
holds because the tree is format-clean and pyproject's `extend-exclude` keeps
the formatter out of markdown and the vendored `.agents/`.
The live checks run on their own daily schedule, never as a gate, split into a
notifying job and a `continue-on-error` job for the endpoints (`ci_hostile`
marker) that answer a datacenter IP differently from a laptop.

**Mutation testing** is tooling, not an automatic gate: `mutmut`, in the
`mutation` dependency group, invoked through `./mutate` at the repo root and
pointed at one module at a time — the manual audit step before tagging a
release that touched pipeline logic. It is the only instrument
that catches a test which passes with the code it claims to protect reverted,
and it has already found real gaps here. A survivor is a candidate, not a
verdict — an equivalent mutant that rewrites a `reason` string changes no
behaviour under contract.

Suite structure: `tests/` mirrors `src/dex_engine/`
(`tests/drivers/test_x.py`, `tests/pipeline/test_ledger.py`,
`tests/render/test_kernel.py`; payload fixtures under
`tests/fixtures/<driver>/`). `tests/conftest.py` provides an
`instance(tmp_path)` fixture building the corpus/state/enrichment skeleton
(trivial once `Instance` is injected), a `FakeDriver` factory, and a
`FlippableProvider` whose `available()` the waiting-cohort tests toggle.
Driver/provider Protocol conformance is verified by the typed registry
literal (§2) — no separate conformance tests needed.

## 16. Cleanup round: behaviour-neutral debts

Debts the build left in the engine, batched so they aren't lost. Owner
ruling: these ship **before the first release**, which is what makes them
release work and puts them here rather than on the roadmap. They were
sequenced around the driver-outcome change (§2) — the cleanup touches the
same seams, and the outcome union settled several of them on its way
past.

- **One shared fetch-and-classify helper.** Nine hand-rolled copies across
  the drivers, `transcribe` and `run`. **Done**: `drivers/fetch.py` —
  `fetch_classified` returns the response or a `FetchFailure` carrying
  the classification together with the wire status, so the callers that
  route the classification onward and the two that must not inherit its
  framing (a signed caption track, the wayback-rescue note) share one
  pairing; only `paper`'s full-text fallback still calls the transport
  bare, because it classifies nothing by design (a miss degrades to
  abstract-only).
- **Duplicated body / slug / extension helpers.** youtube `_body` vs
  `transcribe.youtube_body`; `capture` vs `normalize` for `URL_RE` and
  slugify; `run._ext_of` vs `transcribe._audio_ext`. **Done**: the
  transcript-body composition lives in `pipeline/enrichment.py` (the
  youtube driver and the drain both consume it — the section headings and
  the `via` field are one contract); `URL_RE` and `slugify` live in
  `pipeline/capture.py` and `normalize` imports them; the extension guess
  is `urls.ext_of`, its per-family default (`jpg` media, `mp3` audio)
  stated by the caller.
- **Six spellings of Classification→Result.** One conversion, once.
  **Done**, and carried through the outcome union as
  `Classification.to_outcome()` in `classify.py`, where the
  classification layer meets the driver seam; every former spelling calls
  it, and the wayback fallback appends its rescue note onto the
  classification's reason before converting through the same method.
- **One enrichment-frontmatter module owning render and parse.** The writer
  lives in `run.py`, the reader in `transcribe.py`; the §1 format is one
  contract and belongs to one module. **Done**: `pipeline/enrichment.py`
  renders the file and parses it back as inverses over the one scalar
  unquoting rule (`frontmatter.py`, whose digest-side readers are
  untouched), keeping both readers' deliberately different
  unterminated-fence contracts — the drain's quiet no-fields answer and
  the scan's loud raise.
- **Hoist the yt-dlp seam out of `drivers/youtube.py`** so
  `pipeline/transcribe.py` stops importing a driver — the standing
  violation of the driver-isolation rule (§2) inside the engine's own
  code. **Done**: `drivers/ytdlp.py` (probe, audio download, the probe
  failure vocabulary, the audio-cache scan) serves the youtube driver and
  the transcribe drain from the lib layer; outside `registry.py` — the
  conformance point (§14) — no pipeline module imports a driver.
- **`ledger.py`'s per-word legacy-vocabulary hint tables.** **Done**: the
  tables are the boundary's public `RENAMED_KINDS` / `RETIRED_STATUSES`,
  and migration 1 translates from those same objects (its filename
  prefixes derived from the kind renames), so the loud pre-migration
  error and the translation are one spelling — the hint can never name a
  word the migration does not fix, nor miss one it does. The pinned error
  messages are byte-identical.
- **The import-time `DRIVERS` registry.** A module-level list against the
  no-import-time-state rule (§14); the typed registry literal is still the
  Protocol-conformance point (§2), just not at import. **Done**:
  `registry.default_drivers()` builds the default list on demand and the
  entry points call it — `run._unit_owners`'s standing-report callers,
  normalize (threaded once per run), lint, exclude, and migration 2 (once
  per apply) — so importing `registry` constructs nothing; the typed
  literal in `build_drivers` remains the conformance point, and the web
  catch-all stays last in the one place the order is defined.
- **Streaming transport reads.** An unbounded `response.read()` buffers a
  whole enclosure in memory; enforce the §7 media cap in-stream instead, so
  an oversize download is refused while it arrives rather than after.
  **Done**: the transport takes the caller's byte ceiling (`limit`) and
  reads the body in chunks to at most one byte past it, so `len(body) >
  limit` still fires while the rest is never drawn; `fetch_classified`
  carries the ceiling through, and the media stage passes the §7 10MB cap
  on its GET — an oversize body behind a lying or absent Content-Length
  now costs ~10MB of memory, not the body. Under-ceiling bodies are
  byte-identical and the over-ceiling outcome is the same `skipped` with
  the same reason. The ceiling applies exactly where one exists: the
  transcribe drain's enclosure GET stays whole-body by design (a 150MB
  episode is the work, and its bytes go to the audio cache), as do
  inbox's asset download (size known from the API and verified after; the
  capture must materialize whole) and the JSON API reads — none of these
  has a ceiling to enforce.
- **A per-item fetched-count cache.** The cap check recounts the item's
  entries per admission, which is quadratic in the item's ledger. **Done**:
  the drain keeps a per-item table built lazily from the entries, updated
  in `record` — the one door entries change through — and dropped whenever
  ownership re-resolves, since the counts attribute exactly as `owner_of`
  does. At every read it equals the full recount (pinned across
  admissions, cap fires, `--force`, budget-returning skips, media lines,
  and a rename's re-attribution); the cap check is O(1) per admission
  after one build.
- **One long-lived append handle for the ledger.** **Done**:
  `ledger.Appender` holds one append-mode handle for a drain's many
  writes (the verbs close it as they finish; `ledger.append` remains as
  its one-shot form for the single-line callers). Every line is flushed
  as written, so the durability contract of open-per-line is kept
  exactly — after each append the line is with the OS, visible to every
  reader and safe against a process crash; neither shape fsyncs, so an OS
  crash can cost the tail either way, as before. The file format is
  byte-identical and append mode keeps an interleaved writer's line
  intact. The per-line reopen turned out to be load-bearing in a second
  way: `compact`, exclude's purge, and the migrations rewrite the ledger
  atomically (temp file + one replace — a new inode at the same path),
  and open-per-line re-resolved the path every write, so post-rewrite
  appends followed the file. The held handle keeps that property by
  revalidating identity per line (`fstat` vs `stat` on dev/ino, reopen
  on mismatch or a briefly missing path) — one stat pair per line, still
  far cheaper than the reopen it replaced; the check must never be
  "optimised" away.

Two seams of this round already landed as part of fixes and are **done**:
`drivers/gh.py`, the authenticated GitHub route the github and file drivers
share (§3), and `drivers/audio.py`, the audio-on-a-page signal the web and
podcast drivers share (§9). Neither driver imports the other through them,
and they are the shape the yt-dlp hoist copies.

## 17. Deferred / out of scope (tracked on the roadmap)

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
