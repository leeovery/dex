"""dex-sync: pin-aware engine sync — step 0 of every dex-run session.

Flow::

    1. read .dex-engine-pin → check the latest release (git ls-remote --tags)
    2. newer release? → bump the pin, RE-EXEC sync at the new tag
       (majors auto-apply too — the always-migratable commitment — announcing
       loudly)
    3. run pending migrations (new code, before anything else touches state)
    4. refresh engine-managed machinery from the running — pinned — version's
       bundled template: .claude/skills/dex-*, .claude/dex-contract.md,
       bin/dex, .gitattributes (instance-owned files are never touched)
    5. render the sync report (one surface)

``.dex-engine-pin`` is one line at the instance root, committed,
instance-owned: sync writes its *value* — it is not a synced-template file.
Bootstrap: the first tag-aware sync pins that sync's own release. Until any
release tag exists (pre-first-tag), sync runs migrations + template sync
without pinning and the shim keeps tracking main; the same holds when the
running engine is newer than every release. A failed release check (offline,
remote unreachable) is a note on the report, never a dead instance — the
next sync retries.

Mint cuts releases and changelogs — human-invoked, never the engine; this
command only consumes them. Rollback is editing the pin line.

Run from the instance root: dex-sync (or bin/dex sync).
"""

import argparse
import datetime
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

from dex_engine import atomic, migrations
from dex_engine.migrations import AppliedMigration
from dex_engine.pipeline.types import Instance, parse_version
from dex_engine.render import surfaces
from dex_engine.version import engine_version

__all__ = [
    "PIN_FILE",
    "REPO_URL",
    "ReleaseChannel",
    "ReleaseCheckError",
    "build_parser",
    "main",
    "read_pin",
    "release_tags",
    "run_sync",
    "sync",
    "write_pin",
]

PIN_FILE = ".dex-engine-pin"
REPO_URL = "https://github.com/leeovery/dex"


class ReleaseCheckError(RuntimeError):
    """The release check could not reach or read the remote's tags."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ReleaseChannel:
    """The seams between sync and the world outside the instance.

    ``ls_remote`` lists the remote's tags (raw ``git ls-remote --tags``
    output); ``execute`` replaces this process with the given argv. Both are
    injected so the bootstrap/bump/re-exec flow is testable without a
    network or a process boundary.
    """

    repo_url: str = REPO_URL
    ls_remote: Callable[[str], str]
    execute: Callable[[list[str]], None]


def _git_ls_remote(repo_url: str) -> str:
    try:
        completed = subprocess.run(  # noqa: S603 — engine-built args, no shell
            ["git", "ls-remote", "--tags", repo_url],  # noqa: S607 — git resolves via PATH like every dev tool
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise ReleaseCheckError(f"git ls-remote --tags {repo_url}: {e}") from e
    return completed.stdout


def _exec_argv(argv: list[str]) -> None:
    """Replace this process with ``argv`` — the production re-exec seam."""
    os.execvp(argv[0], argv)  # noqa: S606 — re-exec at the new tag IS the mechanism


def default_channel() -> ReleaseChannel:
    """The production channel: real git, real process replacement."""
    return ReleaseChannel(repo_url=REPO_URL, ls_remote=_git_ls_remote, execute=_exec_argv)


# ---------------------------------------------------------------------------
# Pin file
# ---------------------------------------------------------------------------


def read_pin(root: Path) -> str | None:
    """The pinned release tag, or ``None`` when unpinned (file absent/empty)."""
    path = root / PIN_FILE
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8").strip() or None


def write_pin(root: Path, tag: str) -> None:
    """Write the pin line — the one place the engine sets the pinned tag.

    Atomic (same-dir temp file, then replace), like every state write: a
    crash mid-write must never leave a truncated pin for the shim to read.
    """
    atomic.write_text(root / PIN_FILE, tag + "\n")


def _validate_pin(pin: str) -> None:
    try:
        parse_version(pin)
    except ValueError as e:
        raise ValueError(
            f"{PIN_FILE} holds {pin!r}, which is not a release tag — repair the pin "
            "line (rollback is editing it)"
        ) from e


# ---------------------------------------------------------------------------
# Release discovery
# ---------------------------------------------------------------------------


def release_tags(listing: str) -> list[tuple[str, tuple[int, ...]]]:
    """Parse ``git ls-remote --tags`` output into ``(tag, version)`` pairs.

    Only version-shaped tags (``v0.2.1`` / ``0.2.1``) are releases; other
    tags (e.g. an instance's standing ``inbox`` release) are ignored. Peeled
    ``^{}`` refs collapse onto their tag.
    """
    found: dict[str, tuple[int, ...]] = {}
    for raw_line in listing.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        _, _, ref = line.partition("\t")
        if not ref.startswith("refs/tags/"):
            continue
        tag = ref.removeprefix("refs/tags/").removesuffix("^{}")
        try:
            found[tag] = parse_version(tag)
        except ValueError:
            continue
    return list(found.items())


def _check_releases(
    channel: ReleaseChannel, notes: list[str]
) -> list[tuple[str, tuple[int, ...]]] | None:
    """The remote's release tags; ``None`` when the check itself failed."""
    try:
        listing = channel.ls_remote(channel.repo_url)
    except ReleaseCheckError as e:
        notes.append(
            f"release check failed ({e}) — proceeding on the current pin/version; "
            "the next sync retries"
        )
        return None
    return release_tags(listing)


# ---------------------------------------------------------------------------
# Pin settlement: bump + re-exec, bootstrap, or proceed
# ---------------------------------------------------------------------------


def _settle_pin(  # noqa: PLR0913 — pin settlement reads every seam: pin, tags, version, channel, echo
    root: Path,
    *,
    pin: str | None,
    tags: list[tuple[str, tuple[int, ...]]],
    running_version: str,
    channel: ReleaseChannel,
    echo: Callable[[str], None],
    notes: list[str],
) -> bool:
    """Settle the pin against the remote's releases; True means re-exec'd.

    The pin is bumped *before* the re-exec, deliberately: if the exec fails,
    the pin already names the new tag and the next shim run lands there.
    """
    if not tags:
        if pin is None:
            notes.append(
                "no release tags on the remote yet — running unpinned, tracking main "
                "(pre-first-release)"
            )
        else:
            notes.append(
                f"pinned {pin}, but the remote lists no release tags — pin left alone; "
                "check the remote"
            )
        return False
    latest_tag, latest_version = max(tags, key=lambda pair: pair[1])
    if pin is not None:
        pin_version = parse_version(pin)
        if latest_version <= pin_version:
            return False
        _announce(
            echo, current=pin, latest_tag=latest_tag, major=latest_version[0] > pin_version[0]
        )
        write_pin(root, latest_tag)
        channel.execute(_reexec_argv(channel.repo_url, latest_tag, previous=pin))
        return True
    running = parse_version(running_version)
    if latest_version > running:
        _announce(
            echo,
            current=f"unpinned, engine {running_version}",
            latest_tag=latest_tag,
            major=latest_version[0] > running[0],
        )
        write_pin(root, latest_tag)
        channel.execute(_reexec_argv(channel.repo_url, latest_tag, previous=None))
        return True
    own = next((tag for tag, version in tags if version == running), None)
    if own is not None:
        write_pin(root, own)
        notes.append(f"first tag-aware sync — pinned this engine's own release {own}")
    else:
        notes.append(
            f"running engine {running_version} is newer than the latest release "
            f"{latest_tag} — left unpinned until it is released"
        )
    return False


def _announce(echo: Callable[[str], None], *, current: str, latest_tag: str, major: bool) -> None:
    if major:
        banner = "=" * 68
        echo(banner)
        echo(f"MAJOR ENGINE UPGRADE: {current} → {latest_tag}")
        echo("Auto-applying — the always-migratable commitment. Migrations")
        echo("run before anything touches state; review the sync report closely.")
        echo(banner)
    echo(
        f"engine release {latest_tag} available (currently {current}) — "
        "bumping the pin and re-running sync at the new tag"
    )


def _reexec_argv(repo_url: str, tag: str, *, previous: str | None) -> list[str]:
    argv = ["uvx", "--from", f"git+{repo_url}@{tag}", "dex-sync"]
    if previous is not None:
        argv += ["--previous-pin", previous]
    return argv


# ---------------------------------------------------------------------------
# Template sync — the machinery refresh dex-new also uses
# ---------------------------------------------------------------------------


def _bundled_template() -> Traversable:
    return resources.files("dex_engine") / "instance"


def _write_if_changed(root: Path, dest: Path, content: str, changed: list[str]) -> None:
    if dest.exists() and dest.read_text(encoding="utf-8") == content:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    changed.append(str(dest.relative_to(root)))


def _copy_tree(
    root: Path, src_dir: Traversable, dest_dir: Path, changed: list[str], *, owned: bool = False
) -> None:
    for item in src_dir.iterdir():
        dest = dest_dir / item.name
        if item.is_dir():
            if owned:
                _converge_shape(root, dest, changed, to_dir=True)
            _copy_tree(root, item, dest, changed, owned=owned)
        elif item.is_file():
            if owned:
                _converge_shape(root, dest, changed, to_dir=False)
            _write_if_changed(root, dest, item.read_text(encoding="utf-8"), changed)


def _converge_shape(root: Path, dest: Path, changed: list[str], *, to_dir: bool) -> None:
    """Clear what stands at ``dest`` when its shape contradicts the template's.

    A release may replace a synced file with a same-named directory, or
    fold a directory down to one file. Inside a ``dex-*`` skill the
    template is authoritative — the rule pruning already applies — so the
    conflicting entry is removed, reported as a machinery change, and the
    copy that follows writes the template's shape. Without this the sync
    itself died: ``mkdir`` over the standing file raised
    ``FileExistsError``, and ``read_text`` over the standing directory its
    own ``OSError``. A symlink is unlinked rather than followed, so
    whatever it pointed at is left alone — pruning's rule here too.
    """
    if to_dir:
        # A symlink resolving to a real directory keeps its shape; a file,
        # a symlink to one, or a dangling symlink does not.
        conflict = (dest.is_symlink() or dest.exists()) and not dest.is_dir()
        noun = "directory"
    else:
        conflict = dest.is_dir()
        noun = "file"
    if not conflict:
        return
    if dest.is_dir() and not dest.is_symlink():
        shutil.rmtree(dest)
    else:
        dest.unlink()
    changed.append(f"removed {dest.relative_to(root)} (template ships a {noun} here now)")


def sync(root: Path, template: Traversable | None = None) -> list[str]:
    """Refresh engine-managed machinery from the bundled instance template.

    Copies ``.claude/skills/dex-*`` (recursively), ``.claude/dex-contract.md``,
    ``bin/dex`` (kept executable), and ``.gitattributes`` — and REMOVES any
    ``dex-*`` skill directory the template no longer ships (the ``dex-``
    namespace under ``.claude/skills`` is engine-owned; a retired skill left
    in place would keep loading its stale procedure in sessions). Also
    ensures the gitignored ``cache/`` directory exists — every render
    receipt goes through it, and a pre-existing instance never scaffolded.
    Instance-owned files (CLAUDE.md, README, content, ``.dex-engine-pin``,
    non-``dex-`` skills) are never touched. ``dex-new`` calls this directly
    to seed a fresh instance.

    Args:
        root: The instance root.
        template: The template tree; ``None`` uses the running engine's
            bundled copy (which is what "sync copies from the bundled
            template of the running — pinned — version" means).

    Returns:
        Change descriptions: paths (relative to ``root``) that were written
        because they differed, plus ``removed <path>`` entries for retired
        skills, for files a synced ``dex-*`` skill no longer carries, and
        for entries cleared because the template's shape changed (a file
        where it now ships a directory, or the reverse).
    """
    tpl = template if template is not None else _bundled_template()
    # Ensured here, not only at scaffold: a migrated pre-existing instance
    # never went through dex-new, and the per-item procedure renders every
    # receipt through cache/receipt.json — the session's own write, which
    # cannot create the directory for itself. Idempotent, never a reported
    # change (gitignored ephemera, not machinery).
    (root / "cache").mkdir(exist_ok=True)
    changed: list[str] = []
    template_skills: set[str] = set()
    for skill in (tpl / "skills").iterdir():
        if skill.is_dir():
            template_skills.add(skill.name)
            dest = root / ".claude" / "skills" / skill.name
            engine_owned = skill.name.startswith("dex-")
            # The synced dex-* skill mirrors the template exactly — shape
            # included: a same-named file where the template now ships a
            # directory (or the reverse) is converged, not crashed on. A
            # file the template dropped would otherwise keep loading its
            # stale procedure in every session, forever. Only the dex-*
            # directories sync owns are converged and pruned.
            _copy_tree(root, skill, dest, changed, owned=engine_owned)
            if engine_owned:
                _prune_tree(root, skill, dest, changed)
    _remove_retired_skills(root, template_skills, changed)
    _write_if_changed(
        root,
        root / ".claude" / "dex-contract.md",
        (tpl / "dex-contract.md").read_text(encoding="utf-8"),
        changed,
    )
    _write_if_changed(
        root, root / "bin" / "dex", (tpl / "dex").read_text(encoding="utf-8"), changed
    )
    (root / "bin" / "dex").chmod(0o755)
    _write_if_changed(
        root,
        root / ".gitattributes",
        (tpl / "gitattributes").read_text(encoding="utf-8"),
        changed,
    )
    return changed


def _prune_tree(root: Path, src_dir: Traversable, dest_dir: Path, changed: list[str]) -> None:
    """Remove whatever ``dest_dir`` holds that ``src_dir`` no longer carries.

    Called only inside ``dex-*`` skill directories, which are engine-owned
    whole — nothing instance-owned can live in one. A symlink is unlinked
    rather than followed, so whatever it pointed at is left alone.
    """
    if not dest_dir.is_dir():
        return
    keep = {item.name for item in src_dir.iterdir()}
    subdirs = {item.name for item in src_dir.iterdir() if item.is_dir()}
    for existing in sorted(dest_dir.iterdir()):
        if existing.name not in keep:
            if existing.is_dir() and not existing.is_symlink():
                shutil.rmtree(existing)
            else:
                existing.unlink()
            changed.append(f"removed {existing.relative_to(root)} (retired skill file)")
        elif existing.name in subdirs and existing.is_dir() and not existing.is_symlink():
            _prune_tree(root, src_dir / existing.name, existing, changed)


def _remove_retired_skills(root: Path, template_skills: set[str], changed: list[str]) -> None:
    skills_dir = root / ".claude" / "skills"
    if not skills_dir.is_dir():
        return
    for existing in sorted(skills_dir.iterdir()):
        if (
            existing.is_dir()
            and existing.name.startswith("dex-")
            and existing.name not in template_skills
        ):
            if existing.is_symlink():
                # rmtree refuses symlinks; unlink drops the link and leaves
                # whatever it pointed at alone.
                existing.unlink()
            else:
                shutil.rmtree(existing)
            changed.append(f"removed .claude/skills/{existing.name} (retired engine skill)")


# ---------------------------------------------------------------------------
# The full sync flow
# ---------------------------------------------------------------------------


def run_sync(  # noqa: PLR0913 — the seams are the signature: clocks, version, channel, echo, template
    instance: Instance,
    *,
    running_version: str,
    today: Callable[[], datetime.date],
    now: Callable[[], datetime.datetime],
    channel: ReleaseChannel,
    echo: Callable[[str], None],
    template: Traversable | None = None,
    previous_pin: str | None = None,
    migrate: Callable[[Path], list[AppliedMigration]] | None = None,
) -> str | None:
    """Run the sync flow (pin check → migrations → template → report) at ``instance.root``.

    Args:
        instance: The instance.
        running_version: The running engine's version (injected).
        today: Injected date clock.
        now: Injected UTC write-instant clock — migrations that seed ledger
            lines stamp it, so their writes order against another machine's.
        channel: Release-check and re-exec seams.
        echo: Where progress and the loud major announcement go (print in
            production) — distinct from the returned report.
        template: Template override for tests; ``None`` uses the bundled one.
        previous_pin: Set by the re-exec after a pin bump (via
            ``--previous-pin``) so the report can show the transition.
        migrate: Migration-runner override for tests; ``None`` runs the
            shipped migrations via :func:`dex_engine.migrations.run_pending`.

    Returns:
        The rendered sync report, or ``None`` when this process re-exec'd
        onto a newer tag (only observable through a fake ``execute`` seam —
        the production seam never returns).

    Raises:
        ValueError: The pin line is not a release tag, or state is invalid.
        RuntimeError: A migration or the applied-migrations log failed.
    """
    root = instance.root
    notes: list[str] = []
    pin = read_pin(root)
    if pin is not None:
        _validate_pin(pin)
    tags = _check_releases(channel, notes)
    if tags is not None and _settle_pin(
        root,
        pin=pin,
        tags=tags,
        running_version=running_version,
        channel=channel,
        echo=echo,
        notes=notes,
    ):
        return None
    if migrate is not None:
        applied = migrate(root)
    else:
        applied = migrations.run_pending(root, today=today, now=now, engine_version=running_version)
    changed = sync(root, template=template)
    notes.extend(rel if rel.startswith("removed ") else f"refreshed: {rel}" for rel in changed)
    if changed or read_pin(root) != pin:
        notes.append("review + commit the refreshed files and the pin")
    payload = _report_payload(
        pin=read_pin(root),
        previous=previous_pin,
        applied=applied,
        machinery_changes=len(changed),
        notes=notes,
    )
    return surfaces.render("sync-report", payload)


def _report_payload(
    *,
    pin: str | None,
    previous: str | None,
    applied: list[AppliedMigration],
    machinery_changes: int,
    notes: list[str],
) -> dict[str, object]:
    migrations_payload: list[dict[str, object]] = [
        {
            "number": one.number,
            "intent": one.intent,
            "actions": list(one.report.actions),
            "skipped": [{"what": s.what, "why": s.why} for s in one.report.skipped],
            "anomalies": list(one.report.anomalies),
        }
        for one in applied
    ]
    payload: dict[str, object] = {
        "migrations": migrations_payload,
        "machinery_changes": machinery_changes,
    }
    if pin is not None:
        payload["pin"] = pin
        if previous is not None and previous != pin:
            payload["previous"] = previous
            if _is_major_bump(previous, pin):
                payload["major"] = True
    if notes:
        payload["notes"] = notes
    return payload


def _is_major_bump(previous: str, pin: str) -> bool:
    """Whether the pin transition crossed a major version.

    ``--previous-pin`` arrives from argv, so an unparseable value reads as
    not-major rather than failing the report the re-exec exists to render.
    """
    try:
        return parse_version(pin)[0] > parse_version(previous)[0]
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# CLI — parse, build, call; zero business logic
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """The argparse tree: dex-sync takes no operational flags."""
    parser = argparse.ArgumentParser(
        prog="dex-sync",
        description="Sync engine-managed machinery into the instance at cwd: "
        "pin check, migrations, template refresh, sync report.",
    )
    parser.add_argument(
        "--previous-pin",
        default=None,
        help="internal: set by the re-exec after a pin bump so the report shows the transition",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """Parse, build the seams, run the sync flow from the instance at cwd."""
    args = build_parser().parse_args(argv)
    instance = Instance(root=Path.cwd())
    try:
        report = run_sync(
            instance,
            running_version=engine_version(),
            today=datetime.date.today,
            now=lambda: datetime.datetime.now(datetime.UTC),
            channel=default_channel(),
            echo=print,
            previous_pin=args.previous_pin,
        )
    except (OSError, ValueError, RuntimeError) as e:
        sys.exit(f"dex-sync: {e}")
    if report is not None:
        sys.stdout.write(report if report.endswith("\n") else report + "\n")


if __name__ == "__main__":
    main()
