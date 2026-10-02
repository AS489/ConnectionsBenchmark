#!/usr/bin/env bash
# Play one puzzle with every model in the free roster, one model at a time.
#
# Design notes:
#  - RESUMABLE. `connbench run` skips any (date, model, attempt) that already has a
#    record, so re-running after an interruption or a quota reset picks up where it
#    left off. Just run it again.
#  - COMMITS AS IT GOES. Each record is committed the moment it is written. A run
#    record is data — including a failed one — and must never be discarded because
#    the batch was interrupted.
#  - TOLERATES FAILURE. One model 429ing or erroring never stops the others; the
#    failure is recorded as an `error` run and excluded from stats by scoring.py.
#  - BOUNDED PER GAME. provider.py caps each REQUEST at 600s, but a game makes up
#    to 12 requests with up to 7 retries each, so nothing bounded a whole game. On
#    2026-09-30 nemotron-3-nano-omni ran 2h03m on one puzzle (18 min the day
#    before) and had to be killed by hand; the two models queued behind it would
#    otherwise have been starved of the day's quota. GAME_TIMEOUT stops that.
#
# Usage: scripts/run_free_batch.sh [YYYY-MM-DD]
set -uo pipefail

DATE="${1:-$(date -u +%F)}"
ROSTER="runner/models-free.yaml"
GAME_TIMEOUT="${GAME_TIMEOUT:-1500}"   # 25 min; typical game is 1-20 min
LOG="data/runs/${DATE}/_batch.log"
mkdir -p "data/runs/${DATE}"

SLUGS=$(python3 -c "
import yaml,sys
for m in yaml.safe_load(open('$ROSTER'))['models']: print(m['slug'])")
TOTAL=$(echo "$SLUGS" | wc -l | tr -d ' ')

echo "=== ConnectionsBench free batch | date=$DATE | $TOTAL models | started $(date -u +%FT%TZ)" | tee -a "$LOG"
i=0
for slug in $SLUGS; do
  i=$((i+1))
  if [ -f "data/runs/${DATE}/${slug}.json" ]; then
    echo "[$i/$TOTAL] skip   $slug (already have a record)" | tee -a "$LOG"
    continue
  fi
  echo "[$i/$TOTAL] start  $slug  $(date -u +%T)" | tee -a "$LOG"
  START=$(date +%s)
  # A stalled game must not eat the day's quota window. Implemented in pure bash
  # because macOS ships no `timeout` (that is coreutils) and this must not depend
  # on an optional install.
  TMPOUT=$(mktemp)
  connbench run --date "$DATE" --models "$ROSTER" --model "$slug" > "$TMPOUT" 2>&1 &
  GAME_PID=$!
  # Watchdog on WALL-CLOCK time, not sleep(1) time. A single `sleep $GAME_TIMEOUT`
  # looks right but is wrong on a laptop: sleep(1) does not advance while the
  # system is suspended, so the budget silently stretches by however long the
  # machine naps. Observed 2026-10-01 — a 1500s limit fired at 2939s after a
  # macOS Maintenance Sleep. Polling and comparing `date +%s` counts suspend
  # against the budget, which is what an unattended overnight run needs.
  ( WD_START=$(date +%s)
    while kill -0 "$GAME_PID" 2>/dev/null; do
      if [ $(( $(date +%s) - WD_START )) -ge "$GAME_TIMEOUT" ]; then
        kill -TERM "$GAME_PID" 2>/dev/null
        GRACE=$(date +%s)
        while kill -0 "$GAME_PID" 2>/dev/null; do
          [ $(( $(date +%s) - GRACE )) -ge 30 ] && { kill -KILL "$GAME_PID" 2>/dev/null; break; }
          sleep 2
        done
        break
      fi
      sleep 10
    done
  ) 2>/dev/null &
  WATCHDOG_PID=$!
  wait "$GAME_PID"; RC=$?
  kill "$WATCHDOG_PID" 2>/dev/null
  wait "$WATCHDOG_PID" 2>/dev/null
  cat "$TMPOUT" | tee -a "$LOG"
  rm -f "$TMPOUT"
  ELAPSED=$(( $(date +%s) - START ))
  # 143 = SIGTERM, 137 = SIGKILL — both mean the watchdog fired.
  if [ "$RC" = "143" ] || [ "$RC" = "137" ]; then
    echo "[$i/$TOTAL] TIMEOUT $slug after ${ELAPSED}s (limit ${GAME_TIMEOUT}s) — abandoned" | tee -a "$LOG"
  fi
  echo "[$i/$TOTAL] done   $slug in ${ELAPSED}s" | tee -a "$LOG"

  # Commit immediately — never lose a record to an interrupted batch.
  if [ -f "data/runs/${DATE}/${slug}.json" ]; then
    git add "data/runs/${DATE}/${slug}.json" "$LOG" 2>/dev/null
    git commit -q -m "run: ${slug} on ${DATE}" 2>/dev/null \
      && echo "[$i/$TOTAL] committed $slug" | tee -a "$LOG"
  fi
done

echo "=== batch finished $(date -u +%FT%TZ)" | tee -a "$LOG"
connbench index && git add data/index && git commit -q -m "index: rebuild after ${DATE} free batch" 2>/dev/null
echo "=== summary:"
python3 - "$DATE" <<'PY'
import json, sys
from pathlib import Path
d = Path("data/runs") / sys.argv[1]
rows = []
for p in sorted(d.glob("*.json")):
    r = json.loads(p.read_text())
    rows.append((r["model"], r["status"], r["groups_solved"], r["mistakes_used"],
                 r["invalid_responses"], round(r["usage"]["latency_ms"]/1000), r["error"]))
print(f"{'model':44} {'status':9} {'grp':>3} {'mis':>3} {'inv':>3} {'secs':>5}  error")
for m, st, g, mi, iv, s, e in rows:
    print(f"{m:44} {st:9} {g:>3} {mi:>3} {iv:>3} {s:>5}  {(e or '')[:40]}")
solved = sum(1 for r in rows if r[1] == "win")
print(f"\n{len(rows)} records | {solved} wins | "
      f"{sum(1 for r in rows if r[1]=='error')} infrastructure errors")
PY
