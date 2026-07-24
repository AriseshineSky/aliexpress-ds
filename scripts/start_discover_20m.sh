#!/usr/bin/env bash
# Mass product_id discovery toward ~20M unique IDs.
#
# Two complementary APIs:
#   1) feed.itemids.get  — IDs only (fast; NO price/rating/sold)
#   2) text.search        — has price + rating + sold(orders); slower / shallower
#
# Usage:
#   ./scripts/start_discover_20m.sh              # itemids loop (default)
#   ./scripts/start_discover_20m.sh itemids
#   ./scripts/start_discover_20m.sh search       # keyword file deep search
#   ./scripts/start_discover_20m.sh both

set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="${HOME}/.local/bin:${PATH}"

MODE="${1:-itemids}"
KW_FILE="${KEYWORDS_FILE:-data/keywords/search_keywords_20m_plain.txt}"
TARGET="${TARGET_UNIQUE:-20000000}"
LOG_DIR="${LOG_DIR:-data/logs}"
# Slower of user 2s vs project conservative DEFAULT (5s) for DS APIs.
MIN_INTERVAL="${MIN_INTERVAL:-5.0}"
mkdir -p "$LOG_DIR" data/keywords

run_itemids() {
  local log="$LOG_DIR/discover_itemids_20m.log"
  echo "[$(date -Is)] starting discover-itemids-loop target=$TARGET → $log"
  nohup uv run aliexpress-ds discover-itemids-loop \
    --target-unique "$TARGET" \
    --max-pages-per-job 5000 \
    --page-size 50 \
    --min-interval "$MIN_INTERVAL" \
    --flush-every 500 \
    --write-es \
    --checkpoint data/discover_itemids_checkpoint.json \
    -o data/discovered_itemids_mass.jsonl \
    >>"$log" 2>&1 &
  echo "pid=$! log=$log"
}

run_search() {
  if [[ ! -f "$KW_FILE" ]]; then
    echo "Missing keywords file: $KW_FILE" >&2
    exit 1
  fi
  local log="$LOG_DIR/discover_search_20m.log"
  local ckpt="${CHECKPOINT:-data/discover_search_20m_checkpoint.json}"
  echo "[$(date -Is)] starting discover-search-loop keywords=$KW_FILE target=$TARGET interval=${MIN_INTERVAL}s → $log"
  # text.search returns price / rating / sold_count (orders). pageSize effectively 20.
  # Interval: max(2s user request, project conservative DEFAULT 5s) → 5s (~0.2 QPS).
  nohup uv run aliexpress-ds discover-search-loop \
    --target-unique "$TARGET" \
    --keywords-file "$KW_FILE" \
    --max-pages-per-keyword 250 \
    --page-size 50 \
    --min-rating 0 \
    --min-interval "$MIN_INTERVAL" \
    --flush-every 200 \
    --write-es \
    --checkpoint "$ckpt" \
    -o data/discovered_search_urls_mass.jsonl \
    >>"$log" 2>&1 &
  echo "pid=$! log=$log checkpoint=$ckpt"
}

case "$MODE" in
  itemids) run_itemids ;;
  search) run_search ;;
  both) run_itemids; run_search ;;
  *)
    echo "Usage: $0 [itemids|search|both]" >&2
    exit 1
    ;;
esac
