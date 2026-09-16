"""``connbench`` command line: ingest / check / show / run / index."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date as _date
from pathlib import Path

from . import puzzles as P
from .provider import MockProvider, OpenRouterProvider, ProviderError, load_models
from .runner import play, run_path, write_record
from .scoring import build_index


def _repo_root() -> Path:
    # runner/src/connbench/cli.py -> repo root is four levels up
    return Path(__file__).resolve().parents[3]


def _add_common(p: argparse.ArgumentParser) -> None:
    root = _repo_root()
    p.add_argument("--data", type=Path, default=root / "data", help="data directory (default: <repo>/data)")


def cmd_ingest(args) -> int:
    root = args.data / "puzzles"
    source = P.LocalFileSource(args.from_file) if args.from_file else P.EyefyreMirror()
    report = P.ingest(source, root, dates=args.date or None, force=args.force)
    print(report.summary())
    for label, reason in report.rejected:
        print(f"REJECTED {label}: {reason}", file=sys.stderr)
    if args.date and not report.written and not report.skipped:
        print(f"no puzzle available yet for {', '.join(args.date)} — exiting cleanly", file=sys.stderr)
    return 0


def cmd_check(args) -> int:
    root = args.data / "puzzles"
    try:
        newest = P.check_staleness(root, max_age_hours=args.max_age_hours)
    except P.StaleArchiveError as e:
        print(f"STALE: {e}", file=sys.stderr)
        return 1
    print(f"ok: newest puzzle {newest}, {len(P.list_dates(root))} archived")
    return 0


def cmd_show(args) -> int:
    puzzle = P.load_puzzle(args.data / "puzzles", args.date)
    print(f"#{puzzle.id}  {puzzle.date}  (source: {puzzle.source})")
    for g in puzzle.groups:
        level = "" if g.level is None else f"[{g.level}] "
        print(f"  {level}{g.name}: {', '.join(g.members)}")
    return 0


def cmd_run(args) -> int:
    puzzles_root = args.data / "puzzles"
    runs_root = args.data / "runs"
    date = args.date or _date.today().isoformat()
    try:
        puzzle = P.load_puzzle(puzzles_root, date)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 1

    models = load_models(args.models)
    if args.model:
        wanted = set(args.model)
        models = [m for m in models if m.slug in wanted]
        missing = wanted - {m.slug for m in models}
        if missing:
            print(f"unknown model slug(s): {', '.join(sorted(missing))}", file=sys.stderr)
            return 2

    if args.provider == "mock":
        def make_provider():
            return MockProvider(args.mock_behaviour, puzzle=puzzle)
    else:
        try:
            shared = OpenRouterProvider()
        except ProviderError as e:
            print(str(e), file=sys.stderr)
            return 2

        def make_provider():
            return shared

    failures = 0
    for cfg in models:
        for attempt in range(1, cfg.attempts_per_day + 1):
            path = run_path(runs_root, date, cfg.slug, attempt)
            if path.exists() and not args.force:
                print(f"skip  {cfg.slug} a{attempt} (exists)")
                continue
            record = play(puzzle, cfg, make_provider(), attempt=attempt, backfill=args.backfill)
            out = write_record(runs_root, record, force=args.force)
            status = record["status"]
            if record["error"]:
                failures += 1
                status = f"ERROR: {record['error'].splitlines()[0]}"
            print(
                f"{status:8} {cfg.slug} a{attempt}  groups={record['groups_solved']} "
                f"mistakes={record['mistakes_used']} invalid={record['invalid_responses']} "
                f"cost=${record['usage']['cost_usd']:.4f}  -> {out}"
            )
    return 1 if failures else 0


def cmd_index(args) -> int:
    stats = build_index(args.data / "runs", args.data / "puzzles", args.data / "index")
    print(json.dumps(stats))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="connbench", description="ConnectionsBench runner")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="fetch, validate and archive puzzles")
    _add_common(p)
    p.add_argument("--from-file", type=Path, help="hand-entered puzzle file in the upstream schema")
    p.add_argument("--date", action="append", help="only ingest these dates (repeatable)")
    p.add_argument("--force", action="store_true", help="overwrite existing archive files")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("check", help="fail if the archive has gone stale")
    _add_common(p)
    p.add_argument("--max-age-hours", type=float, default=48.0)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("show", help="print an archived puzzle")
    _add_common(p)
    p.add_argument("date")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("run", help="play a puzzle with the model roster")
    _add_common(p)
    p.add_argument("--date", help="puzzle date (default: today)")
    p.add_argument("--provider", choices=["openrouter", "mock"], default="openrouter")
    p.add_argument("--mock-behaviour", choices=MockProvider.BEHAVIOURS, default="messy")
    p.add_argument("--model", action="append", help="restrict to these slugs (repeatable)")
    p.add_argument("--models", type=Path, default=_repo_root() / "runner" / "models.yaml")
    p.add_argument("--backfill", action="store_true", help="tag runs as backfill (historical puzzle)")
    p.add_argument("--force", action="store_true", help="re-run even if a record exists")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("index", help="rebuild data/index/*.json from run records")
    _add_common(p)
    p.set_defaults(func=cmd_index)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
