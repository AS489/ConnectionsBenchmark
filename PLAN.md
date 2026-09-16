# ConnectionsBench — Architecture Plan

## Context

There is no public, continuously-updated benchmark that shows how frontier LLMs handle the NYT Connections
puzzle *as it is actually played* — one group at a time, with feedback, under a 4-mistake budget. Existing
efforts are static snapshots over historical puzzles, which are contaminated by training-data memorization.

We are building a daily, forward-looking benchmark. Every morning it fetches that day's puzzle, has a roster of
models play it interactively, scores the results, and publishes a leaderboard, trend charts, and guess-by-guess
replays to a static site. Because puzzles are solved the day they are released, every result is genuinely
out-of-distribution for every model — that is the core value of the design.

`/Users/vaxyzek/Projects/ConnectionsBench` contains nothing but this plan. This is a greenfield build.

**Decisions already made** (confirmed with the user):
- Faithful interactive game loop, not single-shot
- GitHub Actions cron + results committed as JSON + static site (no server, no DB)
- OpenRouter as the single model gateway
- Python runner + TypeScript site

**Verified externally, today (2026-09-08):**
- `Eyefyre/NYT-Connections-Answers` commits "update connections list" daily via GitHub Actions.
  Schema: `{"id": int, "date": "YYYY-MM-DD", "answers": [{"level": int, "group": str, "members": [4 str]}]}`
- **`level` is no longer real data.** NYT's v2 API stopped returning difficulty on 2025-09-20; the mirror
  writes `-1` as a placeholder. Confirmed against today's live entry (id 1180) — all four groups are `-1`.
  Puzzles before 2025-09-20 carry genuine levels 0–3, so only forward-looking daily runs lose it.
- OpenRouter: `POST https://openrouter.ai/api/v1/chat/completions`, `Authorization: Bearer <key>`,
  OpenAI-compatible. Supports strict `response_format: {type: "json_schema", ...}`, a `reasoning`
  parameter (`effort` for OpenAI/Gemini, `max_tokens` for Anthropic/Qwen), and returns `usage` with
  native token counts plus `cost` in credits.

---

## Repository layout

```
ConnectionsBench/
├── .github/workflows/
│   ├── daily.yml            # cron 14:00 UTC — solve today, commit results, trigger deploy
│   ├── backfill.yml         # workflow_dispatch — run a date range / single model
│   ├── test.yml             # push/PR — pytest + mock-provider end-to-end, no API key
│   └── deploy.yml           # build site → GitHub Pages
├── runner/                  # Python 3.12 benchmark engine
│   ├── pyproject.toml
│   ├── models.yaml          # model roster + per-model params
│   ├── src/connbench/
│   │   ├── puzzles.py       # ingest, validate, cache
│   │   ├── game.py          # pure game state machine  ← correctness core
│   │   ├── prompts.py       # system/user templates
│   │   ├── parse.py         # model response → guess
│   │   ├── provider.py      # OpenRouter client + MockProvider
│   │   ├── runner.py        # orchestration, concurrency, retries, idempotency
│   │   ├── scoring.py       # per-run metrics + aggregate index build
│   │   └── cli.py           # typer CLI
│   └── tests/
│       ├── test_game.py
│       ├── test_parse.py
│       └── fixtures/        # real messy model outputs, golden run records
├── data/                    # the "database" — committed JSON
│   ├── puzzles/2026/2026-09-08.json
│   ├── runs/2026-09-08/anthropic__claude-opus-5.json
│   └── index/               # precomputed aggregates the site reads
│       ├── leaderboard.json
│       ├── daily.json
│       └── models.json
└── site/                    # Vite + React + TS + Recharts → static
```

**Why files instead of a DB:** results are append-only and tiny (~4 KB/run; ~15 MB/year at 10 models).
Version-controlled, diffable, reviewable in PRs, and the static site fetches them directly. Shard by year to
keep directory sizes sane. The site never scans `runs/` — the runner precomputes `index/*.json`, so page load
does zero aggregation.

---

## The game engine — `runner/src/connbench/game.py`

Pure, no I/O, no network. This is the piece that must be exactly right, so it is built and tested first.

State: `remaining_words`, `solved[]`, `mistakes` (max 4), `history[]`.

`GameState.guess(words: frozenset[str]) -> GuessResult`:
- Validate: exactly 4 words, all still on the board, not an exact repeat of a prior guess
- Exact match to an unsolved category → `CORRECT`, reveal the category name
- Exactly 3 of the 4 share a category → `ONE_AWAY`, `mistakes += 1`
- Otherwise → `INCORRECT`, `mistakes += 1`
- Terminal: all four solved → `WIN`; `mistakes == 4` → `LOSS`

Rules that need an explicit decision, matching or extending real NYT behavior:
- **Repeat guess** — rejected without penalty, as in the real game, but recorded as `invalid`.
- **Invalid / unparseable output** — up to 2 retries with the error fed back to the model, then the turn is
  charged as a mistake. `invalid_responses` is tracked as its own metric: it measures instruction-following,
  which is a genuine differentiator between models and shouldn't be silently absorbed into the score.
- **Loop guard** — 3 consecutive invalid turns forfeits the game, so a confused model can't burn budget forever.
- **Board order** — deterministic seeded shuffle per `(puzzle_date, model, attempt)`, so presentation order is
  not a confound and every run is exactly reproducible.

## Play loop

Multi-turn chat, full history retained so the model can reason over its own earlier guesses:
- **System**: rules, the 4-mistake budget, the output contract
- **User turn 1**: the 16 shuffled words
- **Assistant**: `{"reasoning": "...", "guess": ["A","B","C","D"]}`
- **User turn N**: feedback (`correct` + revealed category / `one away` / `incorrect`), remaining board,
  mistakes left

Output contract via strict `response_format: json_schema` where the model supports it; `parse.py` provides a
tolerant fallback for the rest — strips code fences, extracts JSON from surrounding prose, normalizes case and
whitespace, and fuzzy-matches returned words back to canonical board words (models routinely reformat them).

## Model roster — `runner/models.yaml`

```yaml
defaults:
  temperature: 0
  max_tokens: 4096
  attempts_per_day: 1
  max_cost_usd_per_run: 0.50
models:
  - slug: anthropic__claude-opus-5
    openrouter_id: anthropic/claude-opus-5
    reasoning: { max_tokens: 8000 }
  - slug: openai__gpt-5
    openrouter_id: openai/gpt-5
    reasoning: { effort: high }
  # ...
```

**Pin dated model snapshots wherever OpenRouter offers them.** If `openai/gpt-5` silently changes underneath
the benchmark, every historical comparison becomes meaningless. Additionally, always record the resolved
`model` string returned in the response body into the run record, so drift is at least detectable after the fact.

## Metrics — `runner/src/connbench/scoring.py`

Per run: `solved`, `groups_solved` (0–4), `mistakes_used`, `guesses_made`, `first_guess_correct`,
`groups_solved_by_name` (which categories fell, and on which turn), `invalid_responses`, `latency_ms`,
`prompt/completion/reasoning_tokens`, `cost_usd`, nullable `error`.

Aggregate: win rate overall and trailing-30-day, mean groups solved, mean mistakes, cost per win, and
**Wilson 95% confidence intervals on every rate**. With only ~30 puzzles a month, raw win rates are extremely
noisy; a bar chart without intervals would actively mislead, so the CI is part of the data model rather than a
later addition.

**Empirical difficulty** replaces NYT's unavailable colour. After all models have played a puzzle, rank its
four groups by how many models solved them and how late in the game — producing a per-puzzle difficulty
ordering derived from the field itself, and a "hardest group" stat. This costs nothing extra (the data is
already in the run records), and is arguably a better signal for an LLM benchmark than a colour calibrated
for humans. Keep `level` in the schema as nullable and populate it for pre-2025-09-20 backfills, where it is
genuine — but never mix it with the empirical measure in the same chart.

Runs with a non-null `error` (infrastructure failure) are excluded from stats. Model failures — bad guesses,
unparseable output, giving up — count as losses. The distinction is enforced in `provider.py`, not guessed at
in scoring.

## Puzzle ingestion — `runner/src/connbench/puzzles.py`

**What the upstream source actually is, and why the design hedges against it:**
`Eyefyre/NYT-Connections-Answers` is one individual's hobby repo — a personal GitHub account, not an
organization; created Feb 2024; 53 stars, 3 forks; **no license file, so no rights are granted**; no NYT
disclaimer. A GitHub Action on their side hits NYT's undocumented `svc/connections/v2/{date}.json` daily and
appends to one `connections.json`. It is a thin automated relay run by a stranger, and it is a bus factor of 1
on our only data feed. The architecture must assume it can vanish without warning.

`PuzzleSource` protocol with pluggable implementations:
1. **Primary**: the Eyefyre mirror raw JSON
2. **Fallback**: manual override file dropped into `data/puzzles/` to hand-enter a day if the feed stalls

**Own the archive from day one.** Every puzzle is normalized and committed to `data/puzzles/<year>/<date>.json`
on the day we fetch it. This is the key mitigation: we continuously accumulate an independent archive we
control, so if the mirror disappears we lose only the ability to obtain *new* puzzles — never our history, and
never the ability to re-score or replay everything already run. Swapping in a replacement source is then a
one-file change behind the protocol.

Recommend *not* scraping `nytimes.com` directly — hitting NYT endpoints from our CI is the part of this design
with real ToS exposure, and the manual override covers the outage case without taking that on.

Validate on ingest — exactly 4 groups, 4 members each, 16 distinct words — and fail loudly rather than
benchmark a malformed puzzle. Treat `level: -1` as null, not as a difficulty.

**Timing**: NYT publishes at midnight ET and the mirror updates once daily, so the cron runs at 14:00 UTC
(10:00 ET), comfortably after both. If the date is missing, the workflow no-ops and exits cleanly instead of
failing; the next day's run backfills it.

## Reliability

- **Idempotent**: a `(date, model, attempt)` with an existing result file is skipped unless `--force`
- **Isolated**: one model's failure never aborts the others; the workflow reports an aggregate status
- **Retries**: exponential backoff on 429/5xx/timeout, capped, then recorded as `error`
- **Budget**: per-run `max_cost_usd` guard plus a daily ceiling that aborts the workflow
- **Concurrency group** on the workflow so a manual backfill can't race the daily commit

## Site — `site/`

Vite + React + TS + Recharts, static build to GitHub Pages, reading `data/index/*.json`.

1. **Leaderboard** — sortable: model, win rate ±CI, avg groups solved, avg mistakes, cost/puzzle, sparkline
2. **Today** — the day's board and a per-model outcome grid, with answers behind a spoiler toggle
3. **Trends** — rolling 14/30-day win rate, multi-series over time
4. **Replay** — guess-by-guess playback for a model+day, showing the model's stated reasoning at each step.
   Nearly free to build, since the full turn history is already in every run record, and it's the most
   compelling view on the site.
5. **Model detail** — per-model history, plus performance against each puzzle's empirically-hardest group

Load the `dataviz` skill before writing any chart code.

---

## Build order

| Phase | Deliverable |
|---|---|
| 0 | Start the daily ingest-and-commit job *immediately*, ahead of everything else — every day it isn't running is a puzzle we can never recover if the mirror goes down |
| 1 | `game.py` + `puzzles.py` + `MockProvider` + full test suite — correctness core, zero API cost |
| 2 | `provider.py` OpenRouter client; one model, one puzzle, live; run-record format frozen |
| 3 | `runner.py` multi-model orchestration, `scoring.py`, index generation, CLI |
| 4 | `daily.yml` / `backfill.yml` / `test.yml` workflows; `OPENROUTER_API_KEY` repo secret |
| 5 | Site: leaderboard + today + trends |
| 6 | Replay view, confidence intervals surfaced, cost dashboard |

## Verification

- `pytest runner/tests` — game rules (one-away, duplicates, win/loss terminals, invalid input), parser against
  the fixtures corpus of real messy outputs, and a golden-file test replaying a recorded run to assert
  identical scoring
- `connbench run --date 2026-09-08 --provider mock` — full pipeline end to end, no key, no cost; this is what
  `test.yml` runs on every PR
- `connbench run --date <today> --model anthropic__claude-opus-5` — one real run; inspect the emitted record
- `connbench index` then `cd site && npm run dev` — confirm the charts render from committed data
- Trigger `daily.yml` manually via `workflow_dispatch` before trusting the cron

## Notes to carry into implementation

- **Backfill contamination**: historical puzzles may sit inside a model's training data. Tag any backfilled run
  `backfill: true` and display it separately from the daily forward-looking results — mixing them silently
  would undermine the benchmark's whole premise.
- **The upstream feed is one person's unlicensed hobby repo.** Never let this dependency become load-bearing
  beyond the day of ingest: fetch, normalize, commit, and from then on read only from our own `data/puzzles/`.
  Add a monitoring step that fails loudly if the mirror hasn't updated in 48h, so a silent feed death is caught
  in days rather than discovered months later as a hole in the data.
- Because the source is unlicensed, treat the puzzle text as third-party content: it's fine to run a benchmark
  against it, but don't present the archive itself as a redistributable dataset.
- The site publishes puzzle answers; keep the current day's answers behind a spoiler toggle.
