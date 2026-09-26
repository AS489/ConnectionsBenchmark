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
#
# Usage: scripts/run_free_batch.sh [YYYY-MM-DD]
set -uo pipefail

DATE="${1:-$(date -u +%F)}"
ROSTER="runner/models-free.yaml"
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
  connbench run --date "$DATE" --models "$ROSTER" --model "$slug" 2>&1 | tee -a "$LOG"
  ELAPSED=$(( $(date +%s) - START ))
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
