import json
from datetime import datetime, timezone

import pytest

from connbench import puzzles as P


def test_canonical_collapses_whitespace_and_case():
    assert P.canonical("  ice   cream ") == "ICE CREAM"
    assert P.canonical("Kayak") == "KAYAK"


def test_normalize_good_record(raw):
    p = P.normalize_record(raw)
    assert p.id == 1187 and p.date == "2026-09-15"
    assert len(p.groups) == 4 and all(len(g.members) == 4 for g in p.groups)
    assert len(set(p.words)) == 16
    assert p.group_of("mole").name == "UNDERCOVER OPERATIVE"


def test_level_minus_one_is_none(raw):
    p = P.normalize_record(raw)
    assert all(g.level is None for g in p.groups)
    assert not p.has_levels


def test_genuine_levels_are_kept(raw_levels):
    p = P.normalize_record(raw_levels)
    assert [g.level for g in p.groups] == [0, 1, 2, 3]
    assert p.has_levels


def test_partial_levels_rejected(raw_levels):
    raw_levels["answers"][2]["level"] = -1
    with pytest.raises(P.PuzzleValidationError, match="partially"):
        P.normalize_record(raw_levels)


def test_levels_must_be_permutation(raw_levels):
    raw_levels["answers"][0]["level"] = 3
    with pytest.raises(P.PuzzleValidationError, match="permutation"):
        P.normalize_record(raw_levels)


def test_levels_out_of_range_rejected(raw_levels):
    raw_levels["answers"][0]["level"] = 7
    with pytest.raises(P.PuzzleValidationError):
        P.normalize_record(raw_levels)


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda r: r["answers"].pop(), "exactly 4 groups"),
        (lambda r: r["answers"][0]["members"].pop(), "exactly 4 members"),
        (lambda r: r["answers"][0]["members"].append("EXTRA"), "exactly 4 members"),
        (lambda r: r["answers"][0]["members"].__setitem__(0, ""), "empty member"),
        (lambda r: r["answers"][0]["members"].__setitem__(0, "   "), "empty member"),
        (lambda r: r["answers"][0]["members"].__setitem__(0, "mole"), "duplicate"),
        (lambda r: r.__setitem__("date", "2026/09/15"), "bad date"),
        (lambda r: r.__setitem__("date", "2026-13-40"), "bad date"),
        (lambda r: r.__setitem__("id", "abc"), "bad id"),
        (lambda r: r["answers"][0].__setitem__("group", ""), "no name"),
    ],
)
def test_validation_failures(raw, mutate, match):
    mutate(raw)
    with pytest.raises(P.PuzzleValidationError, match=match):
        P.normalize_record(raw)


def test_words_are_canonicalized_on_ingest(raw):
    raw["answers"][0]["members"][0] = "  coach "
    p = P.normalize_record(raw)
    assert p.groups[0].members[0] == "COACH"


def test_archive_roundtrip(tmp_path, puzzle):
    root = tmp_path / "puzzles"
    assert P.write_puzzle(root, puzzle) is True
    path = P.archive_path(root, puzzle.date)
    assert path == root / "2026" / "2026-09-15.json"
    loaded = P.load_puzzle(root, puzzle.date)
    assert loaded == puzzle
    doc = json.loads(path.read_text())
    assert doc["groups"][0]["level"] is None


def test_write_is_idempotent(tmp_path, puzzle):
    root = tmp_path / "puzzles"
    assert P.write_puzzle(root, puzzle) is True
    assert P.write_puzzle(root, puzzle) is False
    assert P.write_puzzle(root, puzzle, force=True) is True


def test_ingest_reports_and_skips(tmp_path, raw, raw_levels):
    bad = json.loads(json.dumps(raw))
    bad["date"] = "2025-04-01"
    bad["answers"][0]["members"] = ["", "", "", ""]
    src = P.LocalFileSource(tmp_path / "feed.json")
    src.path.write_text(json.dumps([raw, raw_levels, bad]))
    root = tmp_path / "puzzles"

    r = P.ingest(src, root)
    assert sorted(r.written) == ["2023-06-12", "2026-09-15"]
    assert r.rejected == [("2025-04-01", "group 'INSTRUCT' has an empty member")]
    assert r.summary() == "written=2 skipped=0 rejected=1"

    r2 = P.ingest(src, root)
    assert r2.written == [] and sorted(r2.skipped) == ["2023-06-12", "2026-09-15"]


def test_ingest_date_filter(tmp_path, raw, raw_levels):
    src = P.LocalFileSource(tmp_path / "feed.json")
    src.path.write_text(json.dumps([raw, raw_levels]))
    r = P.ingest(src, tmp_path / "puzzles", dates=["2026-09-15"])
    assert r.written == ["2026-09-15"]


def test_local_source_accepts_single_object(tmp_path, raw):
    src = P.LocalFileSource(tmp_path / "one.json")
    src.path.write_text(json.dumps(raw))
    assert src.fetch() == [raw]
    assert src.name == "manual"


def test_list_dates_sorted(archive):
    assert P.list_dates(archive) == ["2023-06-12", "2026-09-15"]


def test_staleness_ok(archive):
    now = datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc)
    assert P.check_staleness(archive, now=now) == "2026-09-15"


def test_staleness_fires_after_48h(archive):
    now = datetime(2026, 9, 17, 14, 0, tzinfo=timezone.utc)
    with pytest.raises(P.StaleArchiveError, match="2026-09-15"):
        P.check_staleness(archive, now=now)


def test_staleness_on_empty_archive(tmp_path):
    with pytest.raises(P.StaleArchiveError, match="empty"):
        P.check_staleness(tmp_path / "nothing")
