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
echo "--- 0. sync with remote"
# The ingest cron commits to main at 14:00 UTC, so the remote routinely moves
# without anyone touching it. Rebase first or the push at the end is rejected.
if git remote get-url origin >/dev/null 2>&1; then
  if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "working tree is dirty — skipping pull, commit or stash first"
  elif git pull --rebase --autostash 2>&1 | tail -2; then
    :
  else
    echo "pull failed (offline?) — continuing with local state"
  fi
fi

echo
echo "--- 1. ingest"
connbench ingest
# Use `git status --porcelain`, NOT `git diff`: a newly ingested puzzle is an
# UNTRACKED file, which `git diff` does not report. That bug meant this step never
# committed anything, and the ingest cron quietly covered for it until the two
# collided on 2026-09-30 and blocked a rebase.
if [ -n "$(git status --porcelain -- data/puzzles)" ]; then
  git add data/puzzles && git commit -q -m "ingest: $(date -u +%F)" && echo "committed new puzzles"
else
  echo "no new puzzles to commit"
fi

echo
echo "--- 2. staleness check"
if ! connbench check; then
  echo "ARCHIVE IS STALE — the upstream mirror may have stopped updating."
  echo "Investigate before trusting today's data. Continuing anyway."
fi

PUZZLE_FILE="data/puzzles/${DATE:0:4}/${DATE}.json"
echo
echo "--- 2b. resolved target"
echo "    date         : $DATE"
echo "    puzzle file  : $PUZZLE_FILE"
echo "    exists       : $([ -f "$PUZZLE_FILE" ] && echo yes || echo NO)"
echo "    now (UTC)    : $(date -u +%FT%TZ)"
echo "    records held : $(ls "data/runs/$DATE"/*.json 2>/dev/null | wc -l | tr -d ' ')"

if [ ! -f "$PUZZLE_FILE" ]; then
  echo
  echo "No puzzle archived for $DATE — nothing to play. Exiting cleanly (not an error)."
  if [ "$DATE" = "$(date -u +%F)" ]; then
    echo "The NYT publishes at 00:00 ET (04:00 UTC) and the upstream mirror follows"
    echo "some hours later. It is $(date -u +%H:%M) UTC now, so today's puzzle may"
    echo "simply not exist yet — the 15:00 UTC schedule exists for exactly this reason."
  else
    echo "$DATE is not in the archive at all. Check: connbench show $DATE"
  fi
  exit 0
fi

echo
echo "--- 3. play (resumable)"
scripts/run_free_batch.sh "$DATE"

echo
echo "--- 4. index"
connbench index
if [ -n "$(git status --porcelain -- data/index)" ]; then
  git add data/index && git commit -q -m "index: rebuild $(date -u +%F)"
fi

echo
echo "--- 5. push"
if ! git remote get-url origin >/dev/null 2>&1; then
  echo "no remote configured — results are committed locally only"
elif [ -z "$(git log --oneline @{u}..HEAD 2>/dev/null)" ]; then
  echo "nothing new to push"
else
  N=$(git log --oneline @{u}..HEAD | wc -l | tr -d ' ')
  echo "pushing $N commit(s)..."
  if git push 2>&1 | tail -2; then
    echo "pushed"
  else
    # The ingest cron commits to main at 14:00 UTC, so the remote can move while
    # a long batch is running. Rebase onto it and try once more.
    echo "push rejected — remote moved; rebasing and retrying"
    if git pull --rebase --autostash 2>&1 | tail -2 && git push 2>&1 | tail -2; then
      echo "pushed after rebase"
    else
      echo "PUSH FAILED — results are committed locally and safe. Resolve, then: git push"
    fi
  fi
fi

echo
echo "--- done."
git log --oneline -3
