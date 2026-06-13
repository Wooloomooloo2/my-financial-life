"""Automatic rotating database snapshots (ADR-057).

The live ``.mfl`` auto-commits every edit (ADR-016), so *durability* is already
covered — there is no unsaved state to lose. What auto-commit does NOT protect
against is a *logical* mistake: a bad import, a botched bulk-edit, a regression.
Auto-commit persists those just as faithfully. This module keeps a rotating set
of timestamped copies of the live database in a ``Snapshots/`` folder beside it,
so the user can roll back to an earlier state via **File ▸ Open**.

Snapshots are taken on launch, on a periodic in-session timer, and on clean
close (the orchestration lives in ``register_window``). Each is a full,
self-contained copy written through SQLite's online backup API
(``Repository.save_copy``) — WAL-safe and atomic.

Retention is a **grandfather-father-son** (GFS) policy (ADR-060), configurable
per file via the ``setting`` table: keep *every* snapshot within the last
``subdaily_hours`` (today's 30-min captures), then *one per day* back to
``daily_days``, then *one per month* back to ``monthly_months`` — older than that
is dropped. So fine-grained history stays available for today and thins out as it
ages, instead of a flat "keep newest N" that a busy day would exhaust within
hours (losing every older restore point). ``RetentionPolicy`` carries the four
knobs; ``load_policy`` / ``save_policy`` read and write them.

This module is pure and testable: it decides *whether* to snapshot and *where*,
then prunes. ``now`` is injectable so the timestamp logic is deterministic under
test. Everything here is best-effort — a backup that can't be written must never
break the app or block close, so the public entry points swallow failures.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

SNAPSHOT_DIRNAME = "Snapshots"

# Retention defaults (ADR-060). All four are user-configurable per file via the
# `setting` table; these are the out-of-the-box values.
DEFAULT_INTERVAL_MIN = 30      # in-session capture cadence (minutes)
DEFAULT_SUBDAILY_HOURS = 24    # keep EVERY snapshot newer than this
DEFAULT_DAILY_DAYS = 7         # then one-per-day back to this many days
DEFAULT_MONTHLY_MONTHS = 12    # then one-per-month back to this many months

# `setting` keys the policy persists under (per-file, ADR-035 key/value store).
KEY_INTERVAL_MIN = "snapshot_interval_min"
KEY_SUBDAILY_HOURS = "snapshot_subdaily_hours"
KEY_DAILY_DAYS = "snapshot_daily_days"
KEY_MONTHLY_MONTHS = "snapshot_monthly_months"

# Timestamp embedded in the filename. Fixed-width fields mean a lexical sort of
# the filenames is also a chronological sort — relied on by existing_snapshots.
_STAMP_FMT = "%Y%m%d-%H%M%S"
_STAMP_LEN = 15  # len("YYYYMMDD-HHMMSS")


@dataclass(frozen=True)
class RetentionPolicy:
    """The four GFS knobs (ADR-060). Minutes / hours / days / months.

    ``interval_min`` is the in-session capture cadence; the other three define
    the keep-all / one-per-day / one-per-month tiers that :func:`prune` applies.
    """

    interval_min: int = DEFAULT_INTERVAL_MIN
    subdaily_hours: int = DEFAULT_SUBDAILY_HOURS
    daily_days: int = DEFAULT_DAILY_DAYS
    monthly_months: int = DEFAULT_MONTHLY_MONTHS


def _int_setting(repo, key: str, default: int) -> int:
    try:
        return int(repo.get_setting(key))
    except (TypeError, ValueError):
        return default


def load_policy(repo) -> RetentionPolicy:
    """Read the per-file retention policy from the ``setting`` table, falling
    back to the module defaults. Values are clamped to sane floors so a bad
    stored value can never disable retention or make the cadence zero."""
    return RetentionPolicy(
        interval_min=max(1, _int_setting(repo, KEY_INTERVAL_MIN, DEFAULT_INTERVAL_MIN)),
        subdaily_hours=max(1, _int_setting(repo, KEY_SUBDAILY_HOURS, DEFAULT_SUBDAILY_HOURS)),
        daily_days=max(0, _int_setting(repo, KEY_DAILY_DAYS, DEFAULT_DAILY_DAYS)),
        monthly_months=max(1, _int_setting(repo, KEY_MONTHLY_MONTHS, DEFAULT_MONTHLY_MONTHS)),
    )


def save_policy(repo, policy: RetentionPolicy) -> None:
    """Persist the retention policy to the ``setting`` table (per file)."""
    repo.set_setting(KEY_INTERVAL_MIN, str(policy.interval_min))
    repo.set_setting(KEY_SUBDAILY_HOURS, str(policy.subdaily_hours))
    repo.set_setting(KEY_DAILY_DAYS, str(policy.daily_days))
    repo.set_setting(KEY_MONTHLY_MONTHS, str(policy.monthly_months))


def snapshot_dir(db_path: Path | str) -> Path:
    """The ``Snapshots/`` folder beside the live database."""
    return Path(db_path).resolve().parent / SNAPSHOT_DIRNAME


def _snapshot_stem(db_path: Path | str) -> str:
    return Path(db_path).stem


def snapshot_path(db_path: Path | str, now: datetime) -> Path:
    """The snapshot filename for ``db_path`` at ``now`` (no collision check)."""
    stem = _snapshot_stem(db_path)
    return snapshot_dir(db_path) / f"{stem}-{now.strftime(_STAMP_FMT)}.mfl"


def existing_snapshots(db_path: Path | str) -> list[Path]:
    """All snapshot files for this database, oldest first.

    Lexical sort == chronological sort thanks to the fixed-width ``_STAMP_FMT``.
    """
    folder = snapshot_dir(db_path)
    if not folder.is_dir():
        return []
    return sorted(folder.glob(f"{_snapshot_stem(db_path)}-*.mfl"))


def _stamp_of(db_path: Path | str, path: Path) -> datetime | None:
    """Parse the embedded timestamp from a snapshot filename, or None if it
    doesn't match our naming (an unrelated file the user dropped in the folder).

    Tolerates the same-second collision suffix (``…-HHMMSS-1.mfl``) by reading
    only the fixed-width stamp that follows ``{db_stem}-``.
    """
    prefix = _snapshot_stem(db_path) + "-"
    name = path.stem
    if not name.startswith(prefix):
        return None
    try:
        return datetime.strptime(name[len(prefix):][:_STAMP_LEN], _STAMP_FMT)
    except ValueError:
        return None


def _live_mtime(db_path: Path | str) -> float:
    """Newest mtime across the main database file and its WAL sidecar.

    In WAL mode (``repository.py`` opens with ``journal_mode = WAL``) committed
    writes land in ``<db>-wal`` and the main file's mtime only advances on
    checkpoint. So the WAL's mtime — not the main file's — is the true
    'last changed' signal between checkpoints.
    """
    db_path = Path(db_path)
    newest = db_path.stat().st_mtime if db_path.exists() else 0.0
    wal = db_path.with_name(db_path.name + "-wal")
    if wal.exists():
        newest = max(newest, wal.stat().st_mtime)
    return newest


def _changed_since_last_snapshot(db_path: Path | str) -> bool:
    """True if the live db has been written since the newest snapshot.

    Avoids byte-identical duplicates — e.g. launching right after a clean close
    that already snapshotted, where nothing has changed in between.
    """
    snaps = existing_snapshots(db_path)
    if not snaps:
        return True
    return _live_mtime(db_path) > snaps[-1].stat().st_mtime


def _month_index(dt: datetime) -> int:
    """Absolute month number, so month differences are a simple subtraction."""
    return dt.year * 12 + (dt.month - 1)


def prune(
    db_path: Path | str,
    policy: RetentionPolicy,
    now: datetime,
) -> list[Path]:
    """Apply the GFS retention policy (ADR-060). Returns the deleted paths.

    Walking newest→oldest, each snapshot is assigned to a tier by age:

    - **sub-daily** (newer than ``subdaily_hours``): keep *all* — today's
      30-min captures.
    - **daily** (older than that, within ``daily_days``): keep the *newest per
      calendar day*; delete the rest.
    - **monthly** (older than the daily window, within ``monthly_months``): keep
      the *newest per calendar month*; delete the rest.
    - **older than ``monthly_months``**: delete.

    Only ever touches files whose names parse as this database's snapshots
    (:func:`_stamp_of`), so an unrelated file dropped in the folder is left
    alone rather than deleted.
    """
    dated = [
        (p, dt)
        for p in existing_snapshots(db_path)
        if (dt := _stamp_of(db_path, p)) is not None
    ]
    dated.sort(key=lambda pair: pair[1], reverse=True)  # newest first

    subdaily_cutoff = now - timedelta(hours=policy.subdaily_hours)
    daily_cutoff = now - timedelta(days=policy.daily_days)
    now_month = _month_index(now)

    keep: set[Path] = set()
    seen_days: set = set()
    seen_months: set = set()
    for path, dt in dated:
        if dt >= subdaily_cutoff:
            keep.add(path)                       # tier 1: keep all
        elif dt >= daily_cutoff:
            key = dt.date()                      # tier 2: newest per day
            if key not in seen_days:
                seen_days.add(key)
                keep.add(path)
        elif now_month - _month_index(dt) <= policy.monthly_months:
            key = (dt.year, dt.month)            # tier 3: newest per month
            if key not in seen_months:
                seen_months.add(key)
                keep.add(path)
        # else: older than the monthly window — falls through to deletion

    doomed = [path for path, _ in dated if path not in keep]
    for path in doomed:
        try:
            path.unlink()
        except OSError:
            pass
    return doomed


def maybe_snapshot(
    repo,
    *,
    now: datetime | None = None,
    policy: RetentionPolicy | None = None,
    force: bool = False,
) -> Path | None:
    """Write a snapshot of ``repo``'s live database if it has changed, then prune
    to the GFS retention policy (ADR-060).

    ``policy`` defaults to the file's stored policy (:func:`load_policy`).
    Returns the path written, or ``None`` when skipped (nothing changed since the
    last snapshot, unless ``force``) or on any failure. Best-effort by design:
    callers fire this from the launch path, a timer, and ``closeEvent`` and must
    never see it raise.
    """
    now = now or datetime.now()
    try:
        db_path = Path(repo.db_path)
        if policy is None:
            policy = load_policy(repo)
        if not force and not _changed_since_last_snapshot(db_path):
            # Nothing new to capture, but retention may still need to thin an
            # aging set (e.g. yesterday's sub-daily snapshots becoming one
            # daily) — so prune regardless before returning.
            prune(db_path, policy, now)
            return None
        dest = snapshot_path(db_path, now)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Two snapshots within the same second (e.g. a manual Save Copy As right
        # as the timer fires) would collide on the filename — bump with a suffix
        # rather than overwrite an existing snapshot.
        if dest.exists():
            i = 1
            while True:
                alt = dest.with_name(f"{dest.stem}-{i}.mfl")
                if not alt.exists():
                    dest = alt
                    break
                i += 1
        repo.save_copy(dest)
        prune(db_path, policy, now)
        return dest
    except Exception:
        return None
