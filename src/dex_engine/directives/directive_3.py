"""Directive 3: repair what engine 0.2.2's re-reads and transcripts did to stored content.

Engine 0.2.2's migrations 15 to 18 queued a live re-read of content this
instance had stored, and each re-read that landed replaced the stored copy:
some came back better, some worse, and nothing mechanical can tell which.
The same engine transcribed every x post's video, and a video with no speech
came back as invented text. Engine 0.2.3 cancelled the re-reads still
queued and gave back the videos a transcript had deleted; what landed
stands. The judging is the session's, with the earlier copy beside each
current one, and the only repair it makes is from the instance's own git
history.

Everything is read from git at fixed commits: HEAD, which the run's guard
keeps clean before any directive, and the **earlier commit**, the parent of
the one that first recorded migration 15 in ``state/migrations.jsonl``,
which holds this instance as it stood before those migrations ran. The same
survey renders the materials and backs the check, so the list the session
is given is the list the check holds it to. An instance whose history
cannot answer (no repository, a shallow clone, the migrations never run)
has nothing that can be shown worse, so it completes with nothing to do.

Nothing is deleted. A digest drawn from what the session replaced is kept
or revised through the digest verb, the one ``dex-enrich`` verb this
directive permits while it is pending: a digest can hold knowledge nothing
else in the item holds, and an unattended session's permissions refuse to
delete a tracked file.

The check reads the session's record, ``cache/directive-3.md``, against
the files: every listed page, transcript and digest has an outcome, each
outcome is what the file now holds, no digest is gone, and nothing outside
the files the directive names has changed.
"""

import datetime
import re
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from dex_engine.gitread import checkout_bytes, git_output
from dex_engine.pipeline.enrichment import (
    holds_transcript,
    mask_fetched,
    parse_enrichment,
    pre_transcript,
    split_transcript,
)
from dex_engine.pipeline.ledger import LedgerSchemaError, from_line, resolution_key
from dex_engine.pipeline.types import Job, Kind, LedgerEntry, Status, parse_version

__all__ = [
    "INTENT",
    "PERMITS",
    "RECORD",
    "Page",
    "Survey",
    "Transcript",
    "check",
    "materials",
    "survey",
]

INTENT = (
    "repair what engine 0.2.2's re-reads and transcripts did to stored content, from git "
    "history, changing only what is shown to be worse"
)

# The digest verb, so a digest drawn from what the session replaced is
# revised by its writer rather than deleted.
PERMITS = frozenset({"item digest"})

RECORD = "cache/directive-3.md"

_LEDGER = "state/enrichment-ledger.jsonl"
_MIGRATIONS = "state/migrations.jsonl"
_PASSES = "state/passes.jsonl"
_DIGESTS = "state/digests"

# How numbered_log writes the records of the first heal migration and of
# the one that gave back the deleted videos.
_FIRST_HEAL_RECORD = '"number": 15,'
_RESTORE_RECORD = '"number": 20,'
_HEALS = frozenset({"migration-15", "migration-16", "migration-17", "migration-18"})
_RESTORED = "migration-20"

# The reason 0.2.2 closed a video's media unit with when a transcript
# landed; frozen, because what matters is what that engine wrote.
_RETIRED = "superseded — the transcript of its post stands for this video"

# x posts' video was first transcribed by 0.2.2, and first asked for speech
# before any transcriber heard it by 0.2.6: a transcript either side of that
# range was not written from unasked silence.
_FIRST_TRANSCRIBING = (0, 2, 2)
_FIRST_ASKING = (0, 2, 6)

# What a merge adds goes under this heading, dated by the earlier copy's
# fetch, so nothing the site has since dropped reads as the page's current
# state.
_MERGE_HEADING = "## From the copy saved on "
_UNDATED_MERGE_HEADING = "## From the earlier copy"

# Taking a transcript out rewrites these, and step 2 owns that rewrite.
_TRANSCRIPT_FIELDS = frozenset({"model", "via"})

_WORD_RE = re.compile(r"\w+")
_RECORD_RE = re.compile(
    r"^- `?(?P<path>[^`\s]+?)`?: (?P<outcome>kept|restored|merged|speech|not speech|revised)\b"
)
_PAGE_OUTCOMES = frozenset({"restored", "merged"})
_DIGEST_OUTCOMES = frozenset({"kept", "revised"})

# How much of each change the materials quote; the session reads the rest
# from git when it needs it.
_LOST_WORDS = 24
_LOST_LINES = 12
_GAINED_LINES = 6
_LINE_CHARS = 240
_POST_CHARS = 300
_TRANSCRIPT_CHARS = 600

_NO_HISTORY = "this is no git repository, or git is not installed"
_SHALLOW = "this clone is shallow, and the commit before 0.2.2's migrations lies past its depth"
_NEVER_RAN = "0.2.2's migrations never ran here"


@dataclass(frozen=True, slots=True, kw_only=True)
class Page:
    """A stored page a heal re-read changed, beside its copy from the earlier commit."""

    path: str
    earlier_path: str
    item: str
    url: str
    via: str
    earlier: bytes
    now: bytes
    lost: Counter[str]
    gained: Counter[str]
    words: tuple[int, int]
    fences: tuple[int, int]
    rows: tuple[int, int]
    lost_lines: tuple[str, ...]
    gained_lines: tuple[str, ...]
    digest_since: bool

    @property
    def lost_something(self) -> bool:
        """A word, a code fence or a table row the earlier copy held and this one lacks."""
        return bool(self.lost) or self.fences[1] < self.fences[0] or self.rows[1] < self.rows[0]


@dataclass(frozen=True, slots=True, kw_only=True)
class Transcript:
    """An x post holding a transcript an engine that never asked for speech wrote."""

    path: str
    item: str
    url: str
    engine: str
    post: str
    transcript: str
    digest_since: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class Survey:
    """What 0.2.2 left in one instance, read from its ledger and git history."""

    earlier: str
    pages: tuple[Page, ...]
    unharmed: tuple[Page, ...]
    transcripts: tuple[Transcript, ...]
    gone_digests: tuple[str, ...]
    removed: tuple[tuple[str, str], ...]
    videos: tuple[tuple[str, str], ...]


def materials(root: Path) -> str:
    """The list for this instance: every page, transcript, digest and file to judge."""
    found = survey(root)
    if isinstance(found, str):
        return (
            f"History cannot answer here: {found}. Nothing here can be shown to be worse, "
            "so there is nothing to repair.\n"
        )
    return _render(found)


def check(root: Path) -> list[str]:
    """Every condition the session's record and the files do not meet yet."""
    found = survey(root)
    if isinstance(found, str):
        return []
    return _unmet(root, found)


# ---------------------------------------------------------------------------
# The survey
# ---------------------------------------------------------------------------


def survey(root: Path) -> Survey | str:
    """What 0.2.2 left here, or why history cannot say."""
    earlier, why = _earlier_commit(root)
    if earlier is None:
        return why
    now = datetime.datetime.now(datetime.UTC)
    head = _ledger_at(root, "HEAD")
    live = _latest(head, now=now)
    landed_now = _latest(((n, e) for n, e in head if e.path), now=now)
    landed_before = _latest(((n, e) for n, e in _ledger_at(root, earlier) if e.path), now=now)
    changed: list[Page] = []
    folders: dict[str, str] = {}
    for unit_hash, healed in sorted(_healed(head, now=now).items()):
        before, after = landed_before.get(unit_hash), landed_now.get(unit_hash)
        if before is not None and before.path and after is not None and after.path:
            folders[_folder(before.path)] = _folder(after.path)
        page = _page(root, earlier, healed, after, before)
        if page is not None:
            changed.append(page)
    return Survey(
        earlier=earlier,
        pages=tuple(page for page in changed if page.lost_something),
        unharmed=tuple(page for page in changed if not page.lost_something),
        transcripts=tuple(_transcripts(root, live, landed_now)),
        gone_digests=tuple(_gone_digests(root, earlier, live, landed_now)),
        removed=tuple(_removed(root, earlier, folders)),
        videos=tuple(
            sorted(
                (entry.url, entry.item)
                for entry in live.values()
                if entry.job is Job.MEDIA
                and entry.status is Status.SKIPPED
                and entry.reason == _RETIRED
            )
        ),
    )


def _earlier_commit(root: Path) -> tuple[str, str] | tuple[None, str]:
    """(the parent of the commit that first recorded migration 15, ""), or (None, why none)."""
    if git_output(root, ["rev-parse", "--git-dir"]) is None:
        return None, _NO_HISTORY
    recorded = git_output(
        root, ["log", "--reverse", "--format=%H", f"-S{_FIRST_HEAL_RECORD}", "--", _MIGRATIONS]
    )
    first = (recorded or "").split()
    parent = (
        git_output(root, ["rev-parse", "--verify", "--quiet", f"{first[0]}^"]) if first else None
    )
    if parent is None:
        shallow = git_output(root, ["rev-parse", "--is-shallow-repository"]) == "true\n"
        return None, _SHALLOW if shallow else _NEVER_RAN
    return parent.strip(), ""


def _ledger_at(root: Path, revision: str) -> list[tuple[int, LedgerEntry]]:
    """Every ledger line ``revision`` holds that parses, with its position."""
    text = git_output(root, ["show", f"{revision}:{_LEDGER}"]) or ""
    lines: list[tuple[int, LedgerEntry]] = []
    for position, line in enumerate(text.split("\n")):
        if not line.strip():
            continue
        try:
            lines.append((position, from_line(line)))
        except (LedgerSchemaError, ValueError):
            continue
    return lines


def _latest(
    lines: Iterable[tuple[int, LedgerEntry]], *, now: datetime.datetime
) -> dict[str, LedgerEntry]:
    """The latest of ``lines`` per unit, resolved as ``ledger.load`` resolves them."""
    latest: dict[str, LedgerEntry] = {}
    winning: dict[str, tuple[datetime.datetime, int]] = {}
    for position, entry in lines:
        key = resolution_key(entry, position, now=now)
        if entry.hash in winning and key < winning[entry.hash]:
            continue
        latest[entry.hash] = entry
        winning[entry.hash] = key
    return latest


def _healed(
    lines: list[tuple[int, LedgerEntry]], *, now: datetime.datetime
) -> dict[str, LedgerEntry]:
    """Each unit's latest landing by a heal re-read."""
    return _latest(
        (
            (n, e)
            for n, e in lines
            if e.rerun and e.via in _HEALS and e.status is Status.DONE and e.path
        ),
        now=now,
    )


def _page(
    root: Path,
    earlier: str,
    healed: LedgerEntry,
    landed_now: LedgerEntry | None,
    landed_before: LedgerEntry | None,
) -> Page | None:
    """The unit's page now beside its earlier copy; None where nothing changed or either is gone.

    A post is read without its transcript, which the transcript list
    judges: a post whose text is as it was owes nothing here.
    """
    if landed_now is None or landed_now.path is None:
        return None
    if landed_before is None or landed_before.path is None:
        return None
    now_bytes = checkout_bytes(root, "HEAD", landed_now.path)
    earlier_bytes = checkout_bytes(root, earlier, landed_before.path)
    if now_bytes is None or earlier_bytes is None:
        return None
    now_text = now_bytes.decode("utf-8", "replace")
    earlier_text = earlier_bytes.decode("utf-8", "replace")
    if mask_fetched(now_text) == mask_fetched(earlier_text):
        return None
    before_body, now_body = _readable(earlier_text), _readable(now_text)
    if before_body == now_body:
        return None
    before_words = Counter(_WORD_RE.findall(before_body))
    now_words = Counter(_WORD_RE.findall(now_body))
    item = _item_of(landed_now.path)
    return Page(
        path=landed_now.path,
        earlier_path=landed_before.path,
        item=item,
        url=healed.url,
        via=healed.via or "",
        earlier=earlier_bytes,
        now=now_bytes,
        lost=before_words - now_words,
        gained=now_words - before_words,
        words=(before_words.total(), now_words.total()),
        fences=(_fences(before_body), _fences(now_body)),
        rows=(_rows(before_body), _rows(now_body)),
        lost_lines=tuple(_lines_only_in(before_body, now_body)),
        gained_lines=tuple(_lines_only_in(now_body, before_body)),
        digest_since=_digest_written_since(root, earlier, item),
    )


def _readable(text: str) -> str:
    """An enrichment file's body, without the transcript a transcriber appended."""
    fields, body = parse_enrichment(text)
    return pre_transcript(fields, body)


def _merge_heading(earlier: bytes) -> str:
    fields, _ = parse_enrichment(earlier.decode("utf-8", "replace"))
    fetched = fields.get("fetched")
    return f"{_MERGE_HEADING}{fetched}" if fetched else _UNDATED_MERGE_HEADING


def _lines_only_in(body: str, other: str) -> list[str]:
    """Each distinct non-blank line of ``body`` that ``other`` holds nowhere, in order."""
    held = {line.strip() for line in other.split("\n")}
    only = (line.strip() for line in body.split("\n"))
    return [line[:_LINE_CHARS] for line in dict.fromkeys(only) if line and line not in held]


def _fences(body: str) -> int:
    return sum(1 for line in body.split("\n") if line.lstrip().startswith("```"))


def _rows(body: str) -> int:
    """Table rows holding content; a separator row of dashes holds none."""
    return sum(
        1
        for line in body.split("\n")
        if line.lstrip().startswith("|") and any(char.isalnum() for char in line)
    )


def _item_of(path: str) -> str:
    parts = PurePosixPath(path).parts
    return parts[1] if len(parts) > 2 else ""  # noqa: PLR2004 — enrichment/<item>/<file>


def _folder(path: str) -> str:
    return str(PurePosixPath(path).parent)


def _digest(item: str) -> str:
    return f"{_DIGESTS}/{item}.md"


def _digest_written_since(root: Path, since: str, item: str) -> bool:
    """Whether the item's digest was written in a commit after ``since``."""
    written = git_output(root, ["log", "-1", "--format=%H", f"{since}..HEAD", "--", _digest(item)])
    return bool((written or "").strip())


def _transcripts(
    root: Path, live: dict[str, LedgerEntry], landed_now: dict[str, LedgerEntry]
) -> Iterator[Transcript]:
    """Every x post holding a transcript an engine that never asked for speech wrote."""
    for unit_hash, entry in sorted(live.items()):
        landing = landed_now.get(unit_hash)
        if entry.kind is not Kind.X or entry.job is not None or entry.status is not Status.DONE:
            continue
        if landing is None or landing.path is None or not _unasked(landing.engine):
            continue
        content = checkout_bytes(root, "HEAD", landing.path)
        if content is None:
            continue
        fields, body = parse_enrichment(content.decode("utf-8", "replace"))
        split = split_transcript(fields, body)
        if split is None:
            continue
        item = _item_of(landing.path)
        yield Transcript(
            path=landing.path,
            item=item,
            url=entry.url,
            engine=landing.engine,
            post=split[0],
            transcript=split[1],
            digest_since=_digest_after_transcript(root, landing.path, item),
        )


def _unasked(engine: str) -> bool:
    try:
        version = parse_version(engine)
    except ValueError:
        return False
    return _FIRST_TRANSCRIBING <= version < _FIRST_ASKING


def _digest_after_transcript(root: Path, path: str, item: str) -> bool:
    """Whether the item's digest was written in or after the commit that landed the transcript."""
    added = (
        git_output(root, ["log", "--reverse", "--format=%H", "-S## Transcript", "--", path]) or ""
    ).split()
    written = _last_written(root, _digest(item))
    if not added or written is None:
        return written is not None
    return _descends(root, written, added[0])


def _last_written(root: Path, path: str) -> str | None:
    written = (git_output(root, ["log", "-1", "--format=%H", "--", path]) or "").strip()
    return written or None


def _descends(root: Path, commit: str, ancestor: str) -> bool:
    """Whether ``commit`` is ``ancestor`` or comes after it."""
    return git_output(root, ["merge-base", "--is-ancestor", ancestor, commit]) is not None


def _gone_digests(
    root: Path, earlier: str, live: dict[str, LedgerEntry], landed_now: dict[str, LedgerEntry]
) -> Iterator[str]:
    """Each item a video came back to whose digest was written while the video was gone."""
    recorded = git_output(
        root, ["log", "--reverse", "--format=%H", f"-S{_RESTORE_RECORD}", "--", _MIGRATIONS]
    )
    restored = (recorded or "").split()
    if not restored:
        return
    items = {
        _item_of(landing.path)
        for unit_hash, entry in live.items()
        if entry.via == _RESTORED
        and (landing := landed_now.get(unit_hash)) is not None
        and landing.path
    }
    for item in sorted(items):
        written = _last_written(root, _digest(item))
        if written is None or written == earlier:
            continue
        if _descends(root, written, earlier) and not _descends(root, written, restored[0]):
            yield item


def _removed(root: Path, earlier: str, folders: dict[str, str]) -> Iterator[tuple[str, str]]:
    """(earlier path, item now) of each file a healed item held then and holds nowhere now."""
    for before, after in sorted(folders.items()):
        then = (git_output(root, ["ls-tree", "--name-only", earlier, f"{before}/"]) or "").split()
        now = set((git_output(root, ["ls-tree", "--name-only", "HEAD", f"{after}/"]) or "").split())
        for path in then:
            if f"{after}/{PurePosixPath(path).name}" not in now:
                yield path, PurePosixPath(after).name


# ---------------------------------------------------------------------------
# The materials
# ---------------------------------------------------------------------------


def _render(found: Survey) -> str:
    out = [
        f"Earlier commit: {found.earlier}",
        "",
        (
            "Every earlier copy below is a file as that commit holds it: "
            f"`git show {found.earlier}:<earlier path>` prints one whole."
        ),
        "",
    ]
    owed = (found.pages, found.transcripts, found.gone_digests, found.removed, found.videos)
    if not any(owed):
        out += ["Nothing below asks for work: go to step 5.", ""]
    out += _heading("Pages a re-read replaced", len(found.pages))
    for page in found.pages:
        out += _page_block(page)
    out += _heading("Re-reads that lost nothing", len(found.unharmed))
    out += [f"- `{page.path}`: gained {_words(page.gained.total())}" for page in found.unharmed]
    out += _gap(found.unharmed)
    out += _heading("Transcripts on x posts", len(found.transcripts))
    for transcript in found.transcripts:
        out += _transcript_block(transcript)
    out += _heading("Digests written while a video was gone", len(found.gone_digests))
    out += [f"- `{_digest(item)}`" for item in found.gone_digests]
    out += _gap(found.gone_digests)
    out += _heading("Files a re-read removed", len(found.removed))
    out += [f"- `{path}` (item {item})" for path, item in found.removed]
    out += _gap(found.removed)
    out += _heading("Videos no history holds", len(found.videos))
    out += [f"- {url} (item {item})" for url, item in found.videos]
    return "\n".join(out).rstrip("\n") + "\n"


def _heading(title: str, count: int) -> list[str]:
    return [f"## {title} ({count})", "", *(["Nothing.", ""] if not count else [])]


def _gap(entries: tuple[object, ...]) -> list[str]:
    return [""] if entries else []


def _words(count: int) -> str:
    return f"{count} word" if count == 1 else f"{count} words"


def _page_block(page: Page) -> list[str]:
    digest = (
        "written after the earlier commit"
        if page.digest_since
        else "unchanged since the earlier commit"
    )
    block = [
        f"### `{page.path}`",
        "",
        f"- Re-read by {page.via.replace('-', ' ')}: {page.url}",
        f"- Earlier path: `{page.earlier_path}`",
        (
            f"- Words {page.words[0]} → {page.words[1]} ({page.lost.total()} lost, "
            f"{page.gained.total()} gained); code fences {page.fences[0]} → {page.fences[1]}; "
            f"table rows {page.rows[0]} → {page.rows[1]}"
        ),
        f"- Digest: {digest}",
        f"- Merge heading: `{_merge_heading(page.earlier)}`",
    ]
    if page.lost:
        common = page.lost.most_common(_LOST_WORDS)
        named = ", ".join(f"`{word}`" + (f" ({n})" if n > 1 else "") for word, n in common)
        more = len(page.lost) - len(common)
        block.append(f"- Words lost: {named}" + (f", and {more} more" if more > 0 else ""))
    block += _quoted("Lines of the earlier copy the copy now lacks", page.lost_lines, _LOST_LINES)
    block += _quoted(
        "Lines of the copy now the earlier copy lacked", page.gained_lines, _GAINED_LINES
    )
    return [*block, ""]


def _quoted(label: str, lines: tuple[str, ...], cap: int) -> list[str]:
    if not lines:
        return [f"- {label}: none"]
    shown = f"{cap} of {len(lines)}" if len(lines) > cap else str(len(lines))
    return [f"- {label} ({shown}):", *(f"  > {line}" for line in lines[:cap])]


def _excerpt(text: str, cap: int) -> str:
    """``text`` whole when it fits, else its first ``cap`` characters marked as cut."""
    text = text.strip()
    return text if len(text) <= cap else f"{text[:cap]}… ({len(text)} characters)"


def _transcript_block(transcript: Transcript) -> list[str]:
    after = "after" if transcript.digest_since else "before"
    shown = _excerpt(transcript.transcript, _TRANSCRIPT_CHARS)
    return [
        f"### `{transcript.path}`",
        "",
        f"- Post: {transcript.url}, transcript written by engine {transcript.engine}",
        f"- Digest: written {after} the transcript landed",
        f"- The post: {_excerpt(transcript.post, _POST_CHARS) or '(no text)'}",
        f"- The transcript: {shown}",
        "",
    ]


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


def _unmet(root: Path, found: Survey) -> list[str]:
    unmet: list[str] = []
    record = _record(root, unmet)
    touched: set[str] = set()
    owed: set[str] = set(found.gone_digests)
    for page in found.pages:
        outcome = record.get((page.path, "page"))
        taken_out = record.get((page.path, "transcript")) == "not speech"
        unmet += _page_unmet(root, found.earlier, page, outcome, transcript_out=taken_out)
        if outcome in _PAGE_OUTCOMES:
            touched.add(page.path)
            owed.add(page.item)
    for transcript in found.transcripts:
        outcome = record.get((transcript.path, "transcript"))
        unmet += _transcript_unmet(root, transcript, outcome)
        if outcome == "not speech":
            touched.add(transcript.path)
            owed.add(transcript.item)
    revised = _digests_unmet(root, record, owed, unmet)
    unmet += _outside(root, touched, revised)
    return unmet


def _record(root: Path, unmet: list[str]) -> dict[tuple[str, str], str]:
    """The session's record as (path, list) -> outcome; a doubled line is named.

    An absent record is only an unmet condition where something is listed:
    the entries missing from it say so one by one.
    """
    try:
        text = (root / RECORD).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as e:
        unmet.append(
            f"`{RECORD}` is unreadable ({e.__class__.__name__}): write it again as step 4 says"
        )
        return {}
    record: dict[tuple[str, str], str] = {}
    for line in text.split("\n"):
        match = _RECORD_RE.match(line.strip())
        if match is None:
            continue
        key = (match["path"], _list_of(match["path"], match["outcome"]))
        if key in record and record[key] != match["outcome"]:
            unmet.append(
                f"`{RECORD}` records `{match['path']}` as both {record[key]} and "
                f"{match['outcome']}: keep the one line that is true"
            )
        record[key] = match["outcome"]
    return record


def _list_of(path: str, outcome: str) -> str:
    if path.startswith(f"{_DIGESTS}/"):
        return "digest"
    if outcome in {"speech", "not speech"}:
        return "transcript"
    return "page"


def _page_unmet(
    root: Path, earlier: str, page: Page, outcome: str | None, *, transcript_out: bool
) -> list[str]:
    """Hold a page to its outcome, judging only the page: a post's transcript is step 2's."""
    if outcome not in {"kept", "restored", "merged"}:
        return [f"`{RECORD}` has no kept, restored or merged line for `{page.path}` (step 4)"]
    held = _working(root, page.path)
    if outcome == "restored":
        why = (
            None
            if held == page.earlier
            else (
                f"`{page.path}` is recorded restored but is not its earlier copy byte for byte: "
                f"write it with `git show {earlier}:{page.earlier_path} > {page.path}` and "
                "change nothing in it"
            )
        )
        return [] if why is None else [why]
    text = _decoded(held or b"")
    again = ", then take its transcript out again as step 2 says" if transcript_out else ""
    if outcome == "merged":
        why = _merge_unmet(page, text, transcript_out=transcript_out, again=again)
    elif _page_part(text, transcript_out=transcript_out) == _page_part(
        _decoded(page.now), transcript_out=transcript_out
    ):
        why = None
    else:
        why = (
            f"`{page.path}` is recorded kept but has changed: put it back with "
            f"`git checkout HEAD -- {page.path}`{again}"
        )
    return [] if why is None else [why]


def _page_part(text: str, *, transcript_out: bool) -> tuple[dict[str, str], str]:
    """A page's frontmatter and its body above any transcript."""
    fields, body = parse_enrichment(text)
    notes = pre_transcript(fields, body).rstrip()
    if transcript_out:
        fields = {key: value for key, value in fields.items() if key not in _TRANSCRIPT_FIELDS}
    return fields, notes


def _merge_unmet(page: Page, text: str, *, transcript_out: bool, again: str) -> str | None:
    """Why a merged page is not its copy now plus one section of the earlier copy's lines."""
    heading = _merge_heading(page.earlier)
    fields, body = parse_enrichment(text)
    split = split_transcript(fields, body)
    notes, transcript = split if split is not None else (body, None)
    marker = f"\n{heading}\n"
    padded = f"\n{notes}\n"
    if padded.count(marker) != 1:
        return (
            f"`{page.path}` is recorded merged but holds no one `{heading}` section: add it as "
            "step 1 says, before any `## Transcript` heading, or record what the page holds"
        )
    above, _, section = padded.partition(marker)
    now_fields, now_body = parse_enrichment(_decoded(page.now))
    now_split = split_transcript(now_fields, now_body)
    ignored = _TRANSCRIPT_FIELDS if transcript_out else frozenset()
    kept_fields = {key: value for key, value in fields.items() if key not in ignored}
    now_part = _page_part(_decoded(page.now), transcript_out=transcript_out)
    if (kept_fields, above.strip()) != now_part or (
        transcript is not None and (now_split is None or transcript != now_split[1])
    ):
        return (
            f"`{page.path}` is recorded merged but its copy now has changed: put it back with "
            f"`git checkout HEAD -- {page.path}` and add the section again{again}"
        )
    added = [line.strip() for line in section.split("\n") if line.strip()]
    held_before = {line.strip() for line in _readable(_decoded(page.earlier)).split("\n")}
    stray = next((line for line in added if line not in held_before), None)
    if not added or stray is not None:
        shown = f": `{stray[:_LINE_CHARS]}`" if stray else ""
        return (
            f"`{page.path}` is recorded merged but its `{heading}` section holds a line the "
            f"earlier copy does not, word for word{shown}; put in each passage exactly as the "
            "earlier copy holds it"
            if added
            else f"`{page.path}` is recorded merged but its `{heading}` section adds nothing"
        )
    return None


def _decoded(content: bytes) -> str:
    return content.decode("utf-8", "replace")


def _transcript_unmet(root: Path, transcript: Transcript, outcome: str | None) -> list[str]:
    if outcome not in {"speech", "not speech"}:
        return [f"`{RECORD}` has no speech or not speech line for `{transcript.path}` (step 4)"]
    held = _working(root, transcript.path)
    fields, body = parse_enrichment((held or b"").decode("utf-8", "replace"))
    holds = held is not None and holds_transcript(fields, body)
    if outcome == "speech" and not holds:
        why = (
            f"`{transcript.path}` is recorded speech but holds no transcript: put it back "
            f"with `git checkout HEAD -- {transcript.path}`"
        )
    elif outcome == "not speech" and holds:
        why = (
            f"`{transcript.path}` is recorded not speech but still holds its transcript: "
            "take it out as step 2 says"
        )
    else:
        return []
    return [why]


def _digests_unmet(
    root: Path, record: dict[tuple[str, str], str], owed: set[str], unmet: list[str]
) -> set[str]:
    """Hold each settled digest to its outcome; returns the digests recorded revised."""
    revised: set[str] = set()
    recorded = {path: outcome for (path, kind), outcome in record.items() if kind == "digest"}
    for item in sorted(owed):
        path = _digest(item)
        if path not in recorded and (root / path).exists():
            unmet.append(f"`{RECORD}` has no kept or revised line for `{path}` (step 4)")
    for path, outcome in sorted(recorded.items()):
        held, head = _working(root, path), checkout_bytes(root, "HEAD", path)
        if outcome == "revised" and (held is None or held == head):
            unmet.append(
                f"`{path}` is recorded revised but is unchanged: write it with "
                "`bin/dex enrich item digest --file cache/digest.json` (step 3), or record "
                "it kept when the verb wrote it as it stood"
            )
        elif outcome == "kept" and held != head:
            unmet.append(
                f"`{path}` is recorded kept but has changed: put it back with "
                f"`git checkout HEAD -- {path}`"
            )
        if outcome == "revised":
            revised.add(path)
    return revised


def _outside(root: Path, touched: set[str], revised: set[str]) -> list[str]:
    """Every change in the tree this directive's instructions do not name."""
    status = git_output(
        root, ["--no-optional-locks", "status", "--porcelain=v1", "-z", "--untracked-files=all"]
    )
    if status is None:
        return []
    unmet: list[str] = []
    for path, state in _changes(status):
        if state == "M" and (path in touched or path.startswith("wiki/")):
            continue
        # The digest verb writes both, and creates either where none stood.
        if state in {"M", "?"} and (path in revised or path == _PASSES):
            continue
        if state == "?":
            unmet.append(f"`{path}` is new, and this directive adds nothing: remove it")
            continue
        if state == "D":
            unmet.append(
                f"`{path}` was deleted, and this directive deletes nothing: put it back with "
                f"`git checkout HEAD -- {path}`"
            )
            continue
        unmet.append(
            f"`{path}` changed, and this directive changes only the pages it restores or "
            "merges, the transcripts it takes out, the digests it revises and pages under "
            f"`wiki/`: put it back with `git checkout HEAD -- {path}`"
        )
    return unmet


def _changes(status: str) -> Iterator[tuple[str, str]]:
    """(path, state) per entry of ``git status --porcelain=v1 -z``: M, D, ?, or its letter."""
    records = iter(status.split("\0"))
    for record in records:
        if len(record) < 4:  # noqa: PLR2004 — "XY path" at its shortest
            continue
        code, path = record[:2], record[3:]
        if code[0] in "RC":
            next(records, None)  # a rename or copy names its source next
        if code == "??":
            yield path, "?"
        elif "D" in code:
            yield path, "D"
        elif "M" in code:
            yield path, "M"
        else:
            yield path, code.strip()[:1]


def _working(root: Path, path: str) -> bytes | None:
    try:
        return (root / path).read_bytes()
    except OSError:
        return None
