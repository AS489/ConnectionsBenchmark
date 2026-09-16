"""Per-run metrics and the aggregate index the site reads.

Rules the aggregates enforce:
- Runs with a non-null ``error`` are infrastructure failures and are excluded.
- Model failures (bad guesses, forfeits) count as losses.
- Backfilled runs are aggregated separately and never mixed with daily results.
- Every rate carries a Wilson 95% interval. With ~30 puzzles a month a bare win
  rate would actively mislead.
- Genuine NYT ``level`` and empirical difficulty are never combined.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date as _date, timedelta
from pathlib import Path

from .game import GameState, Outcome, Status

RUN_SCHEMA_VERSION = 1
TRAILING_WINDOW_DAYS = 30


def wilson_interval(successes: int, n: int, z: float = 1.959963984540054) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion. Returns (low, high) in [0, 1]."""
    if n <= 0:
        return (0.0, 0.0)
    p = successes / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return (round(max(0.0, centre - half), 6), round(min(1.0, centre + half), 6))


def score_game(game: GameState) -> dict:
    """Per-run metrics derived purely from the finished game state."""
    solved_by_name = {h.group_name: h.turn for h in game.history if h.outcome is Outcome.CORRECT}
    first = next((h for h in game.history if h.outcome is not Outcome.FAILED_TURN), None)
    return {
        "status": game.status.value,
        "solved": game.status is Status.WIN,
        "groups_solved": len(game.solved),
        "mistakes_used": game.mistakes,
        "guesses_made": game.guesses_made,
        "invalid_responses": game.invalid_responses,
        "first_guess_correct": bool(first and first.outcome is Outcome.CORRECT),
        "groups_solved_by_name": solved_by_name,
    }


# --------------------------------------------------------------------------- index


def _rate(successes: int, n: int) -> dict:
    low, high = wilson_interval(successes, n)
    return {
        "n": n,
        "successes": successes,
        "rate": (successes / n) if n else None,
        "ci_low": low if n else None,
        "ci_high": high if n else None,
    }


def _mean(values) -> float | None:
    values = list(values)
    return (sum(values) / len(values)) if values else None


def load_runs(runs_root: str | Path) -> list[dict]:
    runs_root = Path(runs_root)
    if not runs_root.exists():
        return []
    out = []
    for path in sorted(runs_root.glob("*/*.json")):
        out.append(json.loads(path.read_text(encoding="utf-8")))
    return out


def _model_summary(runs: list[dict], today: _date) -> dict:
    n = len(runs)
    wins = sum(1 for r in runs if r["solved"])
    cutoff = (today - timedelta(days=TRAILING_WINDOW_DAYS)).isoformat()
    recent = [r for r in runs if r["date"] > cutoff]
    total_cost = sum(r["usage"]["cost_usd"] for r in runs)
    return {
        "runs": n,
        "win_rate": _rate(wins, n),
        "trailing_30d_win_rate": _rate(sum(1 for r in recent if r["solved"]), len(recent)),
        "first_guess_rate": _rate(sum(1 for r in runs if r["first_guess_correct"]), n),
        "mean_groups_solved": _mean(r["groups_solved"] for r in runs),
        "mean_mistakes": _mean(r["mistakes_used"] for r in runs),
        "mean_invalid_responses": _mean(r["invalid_responses"] for r in runs),
        "total_cost_usd": total_cost,
        "cost_per_puzzle_usd": (total_cost / n) if n else None,
        "cost_per_win_usd": (total_cost / wins) if wins else None,
        "history": [
            {"date": r["date"], "solved": r["solved"], "groups_solved": r["groups_solved"]}
            for r in sorted(runs, key=lambda r: r["date"])
        ],
        "resolved_models": sorted({r["resolved_model"] for r in runs if r.get("resolved_model")}),
    }


def empirical_difficulty(puzzle_runs: list[dict], group_names: list[str]) -> list[dict]:
    """Rank a puzzle's groups by how the field did on them: fewer solves, then later solves,
    means harder. Independent of NYT's colour, which is unavailable going forward."""
    rows = []
    for name in group_names:
        turns = [r["groups_solved_by_name"][name] for r in puzzle_runs if name in r["groups_solved_by_name"]]
        rows.append(
            {
                "group": name,
                "solved_by": len(turns),
                "solve_rate": (len(turns) / len(puzzle_runs)) if puzzle_runs else None,
                "mean_solve_turn": _mean(turns),
            }
        )
    # hardest first: lowest solve count, then latest mean turn
    rows.sort(key=lambda r: (r["solved_by"], -(r["mean_solve_turn"] or 0)))
    for rank, r in enumerate(rows):
        r["difficulty_rank"] = rank  # 0 = hardest
    return rows


def build_index(runs_root: str | Path, puzzles_root: str | Path, out_dir: str | Path, *, today: _date | None = None) -> dict:
    """Precompute leaderboard.json, daily.json, models.json from committed run records."""
    from .puzzles import load_puzzle

    today = today or _date.today()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_runs = [r for r in load_runs(runs_root) if r.get("error") is None]
    errors = [r for r in load_runs(runs_root) if r.get("error") is not None]
    daily_runs = [r for r in all_runs if not r.get("backfill")]
    backfill_runs = [r for r in all_runs if r.get("backfill")]

    def by_model(runs):
        d: dict[str, list[dict]] = defaultdict(list)
        for r in runs:
            d[r["model"]].append(r)
        return d

    leaderboard = {
        "generated_at": today.isoformat(),
        "window_days": TRAILING_WINDOW_DAYS,
        "daily": {m: _model_summary(rs, today) for m, rs in by_model(daily_runs).items()},
        "backfill": {m: _model_summary(rs, today) for m, rs in by_model(backfill_runs).items()},
        "errors": [{"date": r["date"], "model": r["model"], "error": r["error"]} for r in errors],
    }

    daily: dict[str, dict] = {}
    for r in all_runs:
        day = daily.setdefault(
            r["date"],
            {"date": r["date"], "puzzle_id": r["puzzle_id"], "groups": None, "levels": None, "results": {}, "empirical_difficulty": None},
        )
        day["results"][r["model"]] = {
            "solved": r["solved"],
            "groups_solved": r["groups_solved"],
            "mistakes_used": r["mistakes_used"],
            "invalid_responses": r["invalid_responses"],
            "backfill": bool(r.get("backfill")),
            "cost_usd": r["usage"]["cost_usd"],
        }
    for date, day in daily.items():
        try:
            puzzle = load_puzzle(puzzles_root, date)
        except FileNotFoundError:
            continue
        day["groups"] = [g.to_dict() for g in puzzle.groups]
        day["levels"] = {g.name: g.level for g in puzzle.groups} if puzzle.has_levels else None
        runs_for_day = [r for r in all_runs if r["date"] == date and not r.get("backfill")]
        day["empirical_difficulty"] = empirical_difficulty(runs_for_day, [g.name for g in puzzle.groups])

    models = sorted({r["model"] for r in all_runs} | {r["model"] for r in errors})
    models_doc = {
        "models": [
            {
                "slug": m,
                "openrouter_id": next((r["openrouter_id"] for r in all_runs + errors if r["model"] == m), None),
                "first_run": min((r["date"] for r in all_runs if r["model"] == m), default=None),
                "last_run": max((r["date"] for r in all_runs if r["model"] == m), default=None),
            }
            for m in models
        ]
    }

    (out_dir / "leaderboard.json").write_text(json.dumps(leaderboard, indent=2) + "\n", encoding="utf-8")
    (out_dir / "daily.json").write_text(json.dumps(sorted(daily.values(), key=lambda d: d["date"]), indent=2) + "\n", encoding="utf-8")
    (out_dir / "models.json").write_text(json.dumps(models_doc, indent=2) + "\n", encoding="utf-8")
    return {"runs": len(all_runs), "errors": len(errors), "models": len(models), "days": len(daily)}
