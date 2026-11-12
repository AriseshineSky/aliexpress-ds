#!/usr/bin/env bash
# Daily DS-API URL discovery for the selected AliExpress category roots.
#
# Why: the browser link-crawler stopped in 2026-09 and aliexpress-ds
# discover-search stopped in 2026-07, so the product URL pool (ES
# user1_aliexpress_us_product_urls) stopped growing and enqueue-es had nothing
# new to feed the Redis queue. This job keeps the pool alive via the official
# aliexpress.ds.text.search API (no browser, no captcha).
#
# Only URL docs are written (ES urls index). The hourly enqueue-es timer then
# picks up whatever is missing from user1_aliexpress_us_products.
#
# Category ids are from the DS category tree (`aliexpress-ds categories`), which
# uses different names than the aliexpress.us web tree:
#   Automotive                  -> 34         Automobiles, Parts & Accessories
#   Tools & Home Improvement    -> 1420 + 13  Tools + Home Improvement
#   Sports & Outdoors           -> 18         Sports & Entertainment
#   Pet Supplies                -> 100006664  Home & Garden > Pet Products
#   Office & School Supplies    -> 21         Office & School Supplies
#   Hair Extensions & Wigs      -> 200165144  Hair Extensions & Wigs
#   Baby & Maternity            -> 1501       Mother & Kids
#   Arts, Crafts & Sewing       -> 200003937  Home & Garden > Arts,Crafts & Sewing
#   Motorcycles & Powersports   -> 201355758  Motorcycle Equipments & Parts
#   Business, Industry & Science-> 202216001  Industrial & Business
# Home & Living (4 URLs, not an AliExpress root) and Novelty & Special Use
# (adult tree — repo iron rule #1) are deliberately excluded.
#
# Tunables (env):
#   DISCOVER_MAX_KEYWORDS  auto-generated keywords per category (default 25)
#   DISCOVER_PAGES         pages per keyword (default 3, page_size 20)
#   DISCOVER_MIN_RATING    local rating floor, missing rating kept (default 4.4)
#   DISCOVER_JOBS          override list, entries "label|category_id" joined by ';'
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PATH="${HOME}/.local/bin:${PATH}"

UV="${UV_BIN:-$(command -v uv || echo "${HOME}/.local/bin/uv")}"
APP_DIR="${APP_DIR:-$ROOT}"
OUT_DIR="${OUT_DIR:-$APP_DIR/data/discover}"
MAX_KEYWORDS="${DISCOVER_MAX_KEYWORDS:-25}"
PAGES="${DISCOVER_PAGES:-3}"
MIN_RATING="${DISCOVER_MIN_RATING:-4.4}"
mkdir -p "$OUT_DIR"

DAY="$(date +%Y%m%d)"
LOG="$OUT_DIR/discover_${DAY}.log"
OUT="$OUT_DIR/urls_${DAY}.jsonl"

DEFAULT_JOBS=(
  "Automotive|34"
  "Tools & Home Improvement|1420"
  "Home Improvement|13"
  "Sports & Outdoors|18"
  "Pet Supplies|100006664"
  "Office & School Supplies|21"
  "Hair Extensions & Wigs|200165144"
  "Baby & Maternity|1501"
  "Arts, Crafts & Sewing|200003937"
  "Motorcycles & Powersports|201355758"
  "Business, Industry & Science|202216001"
)

if [[ -n "${DISCOVER_JOBS:-}" ]]; then
  IFS=';' read -r -a JOBS <<<"$DISCOVER_JOBS"
else
  JOBS=("${DEFAULT_JOBS[@]}")
fi

echo "== discover $(date -Is) jobs=${#JOBS[@]} keywords≤${MAX_KEYWORDS} pages≤${PAGES} min_rating=${MIN_RATING}" | tee -a "$LOG"

failed=0
for job in "${JOBS[@]}"; do
  label="${job%%|*}"
  cid="${job##*|}"
  if [[ -z "${label// }" || -z "$cid" ]]; then
    continue
  fi
  echo "-- ${label} (category_id=${cid})" | tee -a "$LOG"
  set +e
  "$UV" run --directory "$APP_DIR" aliexpress-ds discover-search \
    -C "$cid" \
    --max-keywords "$MAX_KEYWORDS" \
    --pages "$PAGES" \
    --min-rating "$MIN_RATING" \
    --write-es \
    --no-enqueue \
    -o "$OUT" >>"$LOG" 2>&1
  rc=$?
  set -e
  if [[ $rc -eq 2 ]]; then
    echo "== DAILY QUOTA exhausted — stopping discovery for today" | tee -a "$LOG"
    break
  elif [[ $rc -ne 0 ]]; then
    echo "-- ${label} FAILED rc=$rc (see $LOG)" | tee -a "$LOG"
    failed=$((failed + 1))
  fi
done

echo "== discover done $(date -Is) failed_jobs=$failed out=$OUT" | tee -a "$LOG"
[[ $failed -ge ${#JOBS[@]} ]] && exit 1
exit 0