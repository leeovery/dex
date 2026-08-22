# Driver outcomes

Agreed design, not yet implemented. Drivers stop deciding lifecycle status.
They report what they found; the orchestrator decides what that means.

## The problem this solves

Every driver currently returns a `Result` carrying a `Status`, so each one
decides the unit's lifecycle for itself. That is the root of the largest
family of defects the review rounds found, and it recurred in six of them:

- private GitHub blobs classified `dead` because an unauthenticated fetch
  404s, condemning live content
- YouTube channel URLs driven as videos, enumerating a whole channel
- Apple podcast episodes unresolvable because the wrong id was looked up
- arxiv spellings splitting into separate work units
- `IncompleteRead` escaping as an engine bug rather than a retryable block
- a "listen to this article" widget rerouting an article to the podcast
  driver, discarding the article

Each was fixed where it was found. The class survives because there is no
single place that decides what a fetch outcome means, and no way to audit
which statuses a driver may produce.

## The contract

A driver is a black box over one source shape. It is passed a work unit,
does its job, and returns an outcome describing what it found:

    Content(body, meta, media, links)   fetched something
    Missing(evidence)                   confirmed gone
    Refused(evidence)                   blocked, paywalled, rate limited
    Unusable(evidence)                  wrong shape, nothing to extract
    NeedsCapability(need)               needs transcription, extraction, OCR
    Redetected(kind, format)            this is not what we thought it was

The driver keeps the knowledge only it has. That a particular yt-dlp
message means confirmed gone, that a 402 means paywalled, that a thin
extraction is not content: those are per-source judgements and they stay in
the driver, carried as evidence on the outcome.

The run layer maps outcome to `Status` in one total match, in one place.
`Missing` is the only road to `dead`. That mapping becomes auditable and
testable on its own, and the question "can this driver produce a terminal
status by accident" stops being answerable only by reading the driver.

## Drivers never raise

Every escape becomes an outcome at the seam. A driver that raises is an
engine bug, not a content problem, and the run layer's single broad except
records it as such. This kills the exception-class defects outright rather
than patching one boundary at a time, which is what the provider-boundary
round had to do for `IncompleteRead`, `HFValidationError`, `IndexError`
from a container with no audio stream, and `csv.Error`.

## Drivers are isolated

A driver never imports another driver. Behaviour two drivers share becomes
a lib beside `drivers/transport.py`, `drivers/gh.py` and `drivers/audio.py`.
The one standing exception is `paper.py`, which delegates to `WebDriver`
wholesale as a fetch strategy rather than borrowing a helper. That needs its
own decision: either the delegation is legitimate and named, or fetch,
wayback and extraction hoist into a lib and the exception goes.

## What this replaces

`Result` becomes the union above rather than a flat dataclass whose fields
are only meaningful for some statuses. Today 204 legal combinations carry
`media` or `children` on a non-done result, and `assets` is validated
done-only while the others are not. Making the wrong states unrepresentable
is the point, not a side effect.

Two related shapes settle with it:

- `LedgerEntry` and `WorkUnit` disagree on what `depth = 0` means, so 828
  of 2484 legal ledger states cannot convert. One rule, stated once.
- `via` carries provenance, routing (`via == "media"` dispatches) and a
  migration marker. Three jobs, one field. Provenance and routing separate.

## Acceptance bar

Each change must name the defect class from the review record that it makes
structurally impossible. Moving code is not enough and line count is not a
criterion. Secondary tests: the rule now lives in exactly one place, the
dependency graph is strictly simpler, and the tests still mean something
afterwards, proven by mutation rather than by passing.

## Not in scope

Inheritance for drivers, a plugin system, and coverage targets. `matches()`
and `canonical()` stay as they are: an ordered chain of responsibility with
a catch-all last, which is proven here.
