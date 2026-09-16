# ConnectionsBench

A daily, forward-looking benchmark of how frontier LLMs play the NYT Connections
puzzle *as it is actually played*: one group at a time, with feedback, under a
4-mistake budget. Every puzzle is solved the day it is released, so every result is
genuinely out-of-distribution for every model.

See [PLAN.md](PLAN.md) for the architecture. This README is the operator's view.

## Layout

```
runner/                 Python engine (pip install -e "./runner[dev]")
  src/connbench/
    puzzles.py          ingest, validation, the committed archive
    game.py             pure game state machine — the correctness core
    parse.py            model response -> four board words (tolerant fallback)
    prompts.py          system prompt, board rendering, feedback, JSON schema
    provider.py         ModelConfig, MockProvider, OpenRouter client
    runner.py           play one puzzle with one model -> run record
    scoring.py          per-run metrics, Wilson intervals, index build
    cli.py              connbench ingest | check | show | run | index
  models.yaml           model roster
  tests/                pytest; fixtures/parse/ is the messy-output corpus
data/
  puzzles/<year>/<date>.json   the archive (the only thing downstream reads)
  runs/<date>/<slug>.json      one record per (date, model, attempt)
  index/*.json                 precomputed aggregates for the site
.github/workflows/
  ingest.yml            14:00 UTC daily: fetch, validate, commit, fail if stale
  test.yml              pytest + mock end-to-end on every push/PR, no API key
```

## Everyday commands

```bash
pip install -e "./runner[dev]"
pytest runner/tests

connbench ingest                       # pull anything new from the mirror into data/puzzles
connbench ingest --from-file day.json  # hand-enter a day the feed missed
connbench check                        # exit 1 if the newest puzzle is >48h old
connbench show 2026-09-15

connbench run --date 2026-09-15 --provider mock         # full pipeline, no key, no network
connbench run --date 2026-09-15 --model openai__gpt-5   # one real run (needs OPENROUTER_API_KEY)
connbench run --date 2024-03-01 --backfill              # historical: tagged, shown separately
connbench index                                         # rebuild data/index/*.json
```

## Rules the code enforces (do not quietly reverse)

- **Board shuffle is a SHA-256 Fisher-Yates**, seeded by `date|model|attempt`.
  `test_shuffle_output_is_pinned` freezes the output; if it ever fails, historical
  runs no longer replay identically. Fix the regression, never the test.
- **A turn is a retry cycle.** Unparseable or illegal responses get the error fed
  back up to twice; only if the whole turn fails is a mistake charged. Three failed
  turns in a row forfeit. `invalid_responses` is its own metric — it measures
  instruction-following and is never folded into the score.
- **Repeat guesses** are rejected without penalty, as in the real game, but counted
  as invalid. All word identity goes through `puzzles.canonical()`.
- **`level: -1` is `None`** — absence of data, not a difficulty. Levels are validated
  all-or-nothing and only exist for puzzles before 2025-09-20.
- **Infrastructure failures are `error` runs**, excluded from stats. Model failures
  — bad guesses, garbage output, forfeits — are losses. The line is drawn in
  `provider.py`, not guessed at in `scoring.py`.
- **Backfilled runs are tagged and aggregated separately.** Historical puzzles may
  be in a model's training data.
- **Every rate carries a Wilson 95% interval.**
- **Never scrape nytimes.com.** The mirror is consulted only on the day of ingest;
  everything downstream reads `data/puzzles/`.

## Known data notes

- 2025-04-01 is unrecoverable from the mirror: the puzzle was all symbols and the
  scraper strips them. It is the only rejected record in the feed; a second
  rejection means the feed changed shape.
- 830 of 1,186 archived puzzles carry genuine difficulty levels.
