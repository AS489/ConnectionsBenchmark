import json
from datetime import date

import pytest

from connbench.provider import MockProvider, ModelConfig
from connbench.runner import play, write_record
from connbench.scoring import build_index, empirical_difficulty, wilson_interval


def test_wilson_known_values():
    lo, hi = wilson_interval(0, 0)
    assert (lo, hi) == (0.0, 0.0)
    lo, hi = wilson_interval(15, 30)
    assert lo == pytest.approx(0.3313, abs=1e-3) and hi == pytest.approx(0.6687, abs=1e-3)
    lo, hi = wilson_interval(30, 30)
    assert lo == pytest.approx(0.8865, abs=1e-3) and hi == 1.0
    lo, hi = wilson_interval(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.2775, abs=1e-3)


def test_empirical_difficulty_orders_hardest_first():
    runs = [
        {"groups_solved_by_name": {"A": 1, "B": 2}},
        {"groups_solved_by_name": {"A": 1, "C": 3}},
        {"groups_solved_by_name": {"A": 2}},
    ]
    rows = empirical_difficulty(runs, ["A", "B", "C", "D"])
    assert [r["group"] for r in rows] == ["D", "C", "B", "A"]
    assert rows[0]["difficulty_rank"] == 0 and rows[0]["solved_by"] == 0
    assert rows[-1]["solve_rate"] == 1.0


def test_build_index(tmp_path, puzzle, archive):
    runs = tmp_path / "runs"
    write_record(runs, play(puzzle, ModelConfig("good", "g"), MockProvider("perfect", puzzle=puzzle)))
    write_record(runs, play(puzzle, ModelConfig("bad", "b"), MockProvider("naive")))
    write_record(runs, play(puzzle, ModelConfig("down", "d"), MockProvider("broken")))
    write_record(runs, play(puzzle, ModelConfig("good", "g"), MockProvider("perfect", puzzle=puzzle), backfill=True, attempt=2))

    stats = build_index(runs, archive, tmp_path / "index", today=date(2026, 9, 16))
    assert stats["runs"] == 3 and stats["errors"] == 1
    assert stats["models"] == 3 and stats["days"] == 1
    assert stats["variants"] == ["v1"] and stats["prompt_warnings"] == []

    lb = json.loads((tmp_path / "index" / "leaderboard.json").read_text())
    assert lb["daily"]["good"]["win_rate"] == {"n": 1, "successes": 1, "rate": 1.0, "ci_low": pytest.approx(0.2065, abs=1e-3), "ci_high": 1.0}
    assert lb["daily"]["bad"]["win_rate"]["rate"] == 0.0
    assert "down" not in lb["daily"]  # error runs are excluded from stats
    assert lb["errors"][0]["model"] == "down"
    assert list(lb["backfill"]) == ["good"]  # backfill kept apart
    assert lb["daily"]["good"]["runs"] == 1

    daily = json.loads((tmp_path / "index" / "daily.json").read_text())
    assert daily[0]["date"] == "2026-09-15"
    assert daily[0]["levels"] is None  # no genuine level for this date
    assert set(daily[0]["results"]) == {"good", "bad"}
    diff = daily[0]["empirical_difficulty"]
    assert len(diff) == 4 and all(r["solved_by"] == 1 for r in diff)

    models = json.loads((tmp_path / "index" / "models.json").read_text())
    assert [m["slug"] for m in models["models"]] == ["bad", "down", "good"]
