#!/usr/bin/env bash
# The once-a-day command. Safe to run repeatedly — every step is idempotent.
#
#   scripts/daily.sh              # today (UTC)
#   scripts/daily.sh 2026-09-26   # a specific day, e.g. to retry throttled models
#
# What it does, in order:
#   1. Ingest — pull any new puzzles from the mirror into data/puzzles/ and commit.
#   2. Check  — fail loudly if the archive has gone stale (>48h), which is the
#               alarm for a silent death of the upstream feed.
#   3. Play   — run every model in the free roster that does not already have a
#               record for this date. Resumable: re-running continues where the
#               last attempt stopped, so models throttled today are simply picked
#               up on the next run.
#   4. Index  — rebuild data/index/*.json and commit.
#
# Free-tier reality: roughly 50 requests/day, and one game costs 5-12. Expect a
# handful of models per day and several days to cover the whole roster.
set -uo pipefail
cd "$(dirname "$0")/.."

DATE="${1:-$(date -u +%F)}"
echo "===================================================================="
echo " ConnectionsBench daily | $DATE | $(date -u +%FT%TZ)"
echo "===================================================================="

echo
echo "--- 1. ingest"
connbench ingest
if ! git diff --quiet -- data/puzzles; then
  git add data/puzzles && git commit -q -m "ingest: $(date -u +%F)" && echo "committed new puzzles"
fi

echo
echo "--- 2. staleness check"
if ! connbench check; then
  echo "ARCHIVE IS STALE — the upstream mirror may have stopped updating."
  echo "Investigate before trusting today's data. Continuing anyway."
fi

if [ ! -f "data/puzzles/${DATE:0:4}/${DATE}.json" ]; then
  echo
  echo "No puzzle archived for $DATE yet — nothing to play. Exiting cleanly."
  exit 0
fi

echo
echo "--- 3. play (resumable)"
scripts/run_free_batch.sh "$DATE"

echo
echo "--- 4. index"
connbench index
if ! git diff --quiet -- data/index; then
  git add data/index && git commit -q -m "index: rebuild $(date -u +%F)"
fi

echo
echo "--- done. Unpushed commits:"
git log --oneline @{u}..HEAD 2>/dev/null || git log --oneline -5
echo
echo "Push with:  git push"
