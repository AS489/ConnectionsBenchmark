#!/usr/bin/env python3
"""Print a one-line-per-model summary of a day's runs.

Used by scripts/run_free_batch.sh and by the daily workflow's run summary.

    scripts/summarize_day.py 2026-10-01
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    date = sys.argv[1] if len(sys.argv) > 1 else None
    if not date:
        print("usage: summarize_day.py YYYY-MM-DD", file=sys.stderr)
        return 2
    d = ROOT / "data" / "runs" / date
    if not d.is_dir():
        print(f"No records for {date} — the puzzle may not be published yet.")
        return 0

    rows = []
    for p in sorted(d.glob("*.json")):
        r = json.loads(p.read_text())
        rows.append(r)

    print(f"{'model':44} {'status':9} {'grp':>3} {'mis':>3} {'inv':>3} {'secs':>5}  note")
    for r in rows:
        note = ""
        if r.get("error"):
            note = r["error"].splitlines()[0][:44]
        elif r.get("budget_starved"):
            note = "budget-starved (never answered)"
        elif r.get("truncated_responses"):
            note = f"{r['truncated_responses']} truncated"
        print(
            f"{r['model']:44} {r['status']:9} {r['groups_solved']:>3} "
            f"{r['mistakes_used']:>3} {r['invalid_responses']:>3} "
            f"{round(r['usage']['latency_ms'] / 1000):>5}  {note}"
        )

    played = [r for r in rows if not r.get("error")]
    wins = sum(1 for r in played if r["solved"])
    print(
        f"\n{len(rows)} records | {len(played)} played | {wins} wins | "
        f"{len(rows) - len(played)} infrastructure errors"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
