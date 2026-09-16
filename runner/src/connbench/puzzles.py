"""Puzzle ingestion, validation, and the committed archive.

Everything downstream of ingest reads only from ``data/puzzles/<year>/<date>.json``.
The upstream mirror is a dependency for exactly one step: obtaining a puzzle on the
day it is published. After that it is never consulted again.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Protocol

EYEFYRE_URL = (
    "https://raw.githubusercontent.com/Eyefyre/NYT-Connections-Answers/main/connections.json"
)

# NYT's v2 API stopped returning difficulty on this date; the mirror writes -1 after it.
LEVELS_END_DATE = "2025-09-20"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class PuzzleValidationError(ValueError):
    """Raised when an upstream record does not describe a well-formed puzzle."""


class StaleArchiveError(RuntimeError):
    """Raised when the newest archived puzzle is older than the allowed age."""


def canonical(word: str) -> str:
    """Canonical identity for a board word: whitespace-collapsed, upper-cased.

    Use this for *every* identity comparison between words. Never compare raw strings.
    """
    return " ".join(str(word).split()).upper()


@dataclass(frozen=True)
class Group:
    name: str
    members: tuple[str, str, str, str]
    level: int | None  # 0-3 when genuine; None when the source carries no difficulty

    def to_dict(self) -> dict:
        return {"name": self.name, "level": self.level, "members": list(self.members)}


@dataclass(frozen=True)
class Puzzle:
    id: int
    date: str
    groups: tuple[Group, Group, Group, Group]
    source: str

    @property
    def words(self) -> tuple[str, ...]:
        """All 16 canonical words, in group order. This is the pre-shuffle order."""
        return tuple(m for g in self.groups for m in g.members)

    def group_of(self, word: str) -> Group:
        w = canonical(word)
        for g in self.groups:
            if w in g.members:
                return g
        raise KeyError(word)

    @property
    def has_levels(self) -> bool:
        return all(g.level is not None for g in self.groups)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "date": self.date,
            "source": self.source,
            "groups": [g.to_dict() for g in self.groups],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Puzzle":
        groups = tuple(
            Group(name=g["name"], members=tuple(g["members"]), level=g.get("level"))
            for g in d["groups"]
        )
        return cls(id=int(d["id"]), date=d["date"], groups=groups, source=d.get("source", "archive"))


# --------------------------------------------------------------------------- validation


def _validate_levels(raw_levels: list) -> list[int | None]:
    """All-or-nothing: either every level is absent (-1/None) or they form a 0-3 permutation.

    A half-populated set means the feed changed shape underneath us, and we want to
    hear about it rather than silently score garbage.
    """
    absent = [lv is None or lv == -1 for lv in raw_levels]
    if all(absent):
        return [None] * len(raw_levels)
    if any(absent):
        raise PuzzleValidationError(f"partially populated levels: {raw_levels}")
    try:
        ints = [int(lv) for lv in raw_levels]
    except (TypeError, ValueError):
        raise PuzzleValidationError(f"non-integer levels: {raw_levels}") from None
    if sorted(ints) != [0, 1, 2, 3]:
        raise PuzzleValidationError(f"levels are not a 0-3 permutation: {raw_levels}")
    return ints


def normalize_record(raw: dict, source: str = "eyefyre") -> Puzzle:
    """Turn one upstream record into a validated Puzzle, or raise PuzzleValidationError.

    Upstream schema: {"id": int, "date": "YYYY-MM-DD",
                      "answers": [{"level": int, "group": str, "members": [4 str]}]}
    """
    if not isinstance(raw, dict):
        raise PuzzleValidationError("record is not an object")

    date = raw.get("date")
    if not isinstance(date, str) or not _DATE_RE.match(date):
        raise PuzzleValidationError(f"bad date: {date!r}")
    try:
        _date.fromisoformat(date)
    except ValueError:
        raise PuzzleValidationError(f"bad date: {date!r}") from None

    try:
        pid = int(raw.get("id"))
    except (TypeError, ValueError):
        raise PuzzleValidationError(f"bad id: {raw.get('id')!r}") from None

    answers = raw.get("answers")
    if not isinstance(answers, list) or len(answers) != 4:
        raise PuzzleValidationError(
            f"expected exactly 4 groups, got {len(answers) if isinstance(answers, list) else answers!r}"
        )

    levels = _validate_levels([a.get("level") if isinstance(a, dict) else None for a in answers])

    groups: list[Group] = []
    seen: set[str] = set()
    for a, level in zip(answers, levels):
        if not isinstance(a, dict):
            raise PuzzleValidationError("group is not an object")
        name = a.get("group")
        if not isinstance(name, str) or not name.strip():
            raise PuzzleValidationError(f"group has no name: {a!r}")
        members = a.get("members")
        if not isinstance(members, list) or len(members) != 4:
            raise PuzzleValidationError(f"group {name!r} does not have exactly 4 members")
        canon: list[str] = []
        for m in members:
            if not isinstance(m, str) or not m.strip():
                raise PuzzleValidationError(f"group {name!r} has an empty member")
            c = canonical(m)
            if c in seen:
                raise PuzzleValidationError(f"duplicate word {c!r}")
            seen.add(c)
            canon.append(c)
        groups.append(Group(name=" ".join(name.split()), members=tuple(canon), level=level))

    return Puzzle(id=pid, date=date, groups=tuple(groups), source=source)


# --------------------------------------------------------------------------- sources


class PuzzleSource(Protocol):
    name: str

    def fetch(self) -> list[dict]:
        """Return raw upstream records. May raise on network failure."""
        ...


class EyefyreMirror:
    """Primary source: the raw connections.json from Eyefyre/NYT-Connections-Answers."""

    name = "eyefyre"

    def __init__(self, url: str = EYEFYRE_URL, timeout: float = 30.0):
        self.url = url
        self.timeout = timeout

    def fetch(self) -> list[dict]:
        req = urllib.request.Request(self.url, headers={"User-Agent": "connbench-ingest"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.load(resp)
        if not isinstance(data, list):
            raise PuzzleValidationError("upstream payload is not a list")
        return data


class LocalFileSource:
    """Fallback source: a hand-entered file in the upstream schema (one record or a list)."""

    name = "manual"

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def fetch(self) -> list[dict]:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list):
            return data
        raise PuzzleValidationError("manual file must contain an object or a list")


# --------------------------------------------------------------------------- archive


def archive_path(root: str | Path, date: str) -> Path:
    return Path(root) / date[:4] / f"{date}.json"


def write_puzzle(root: str | Path, puzzle: Puzzle, *, force: bool = False) -> bool:
    """Write a puzzle to the archive. Returns True if written, False if skipped as existing."""
    path = archive_path(root, puzzle.date)
    if path.exists() and not force:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = puzzle.to_dict()
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return True


def load_puzzle(root: str | Path, date: str) -> Puzzle:
    path = archive_path(root, date)
    if not path.exists():
        raise FileNotFoundError(f"no archived puzzle for {date} at {path}")
    return Puzzle.from_dict(json.loads(path.read_text(encoding="utf-8")))


def list_dates(root: str | Path) -> list[str]:
    root = Path(root)
    if not root.exists():
        return []
    return sorted(p.stem for p in root.glob("*/*.json") if _DATE_RE.match(p.stem))


@dataclass
class IngestReport:
    written: list[str]
    skipped: list[str]
    rejected: list[tuple[str, str]]  # (date-or-id, reason)

    def summary(self) -> str:
        return (
            f"written={len(self.written)} skipped={len(self.skipped)} rejected={len(self.rejected)}"
        )


def ingest(
    source: PuzzleSource,
    root: str | Path,
    *,
    dates: Iterable[str] | None = None,
    force: bool = False,
) -> IngestReport:
    """Fetch from ``source``, validate, and write any new puzzles into the archive.

    Idempotent: puzzles already in the archive are skipped unless ``force``.
    Invalid records are reported, not raised — a single bad day must not block the rest.
    """
    wanted = set(dates) if dates is not None else None
    report = IngestReport(written=[], skipped=[], rejected=[])
    for raw in source.fetch():
        label = str(raw.get("date") or raw.get("id") or "?") if isinstance(raw, dict) else "?"
        if wanted is not None and label not in wanted:
            continue
        try:
            puzzle = normalize_record(raw, source=source.name)
        except PuzzleValidationError as e:
            report.rejected.append((label, str(e)))
            continue
        if write_puzzle(root, puzzle, force=force):
            report.written.append(puzzle.date)
        else:
            report.skipped.append(puzzle.date)
    return report


def newest_date(root: str | Path) -> str | None:
    dates = list_dates(root)
    return dates[-1] if dates else None


def check_staleness(
    root: str | Path, *, now: datetime | None = None, max_age_hours: float = 48.0
) -> str:
    """Return the newest archived date, or raise StaleArchiveError if it is too old.

    This is the alarm for a silent feed death. If it fires, check whether the mirror
    has stopped updating before assuming a bug here.
    """
    newest = newest_date(root)
    if newest is None:
        raise StaleArchiveError("archive is empty")
    now = now or datetime.now(timezone.utc)
    # A puzzle for date D is published at 00:00 ET on D. Measure age from D's midnight UTC,
    # which is conservative in the right direction.
    published = datetime.fromisoformat(newest).replace(tzinfo=timezone.utc)
    age = now - published
    if age > timedelta(hours=max_age_hours):
        raise StaleArchiveError(
            f"newest archived puzzle is {newest} ({age.total_seconds() / 3600:.1f}h old, "
            f"limit {max_age_hours}h)"
        )
    return newest
