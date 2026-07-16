# Coordination: aliexpress-ds (category tree) ↔ aliexpress-link-crawler (URL scrape)
# Shared store: Elasticsearch

## Indices

| Index | Writer | Reader | Content |
|-------|--------|--------|---------|
| `user1_aliexpress_ds_categories` | `aliexpress-ds sync-categories` | ops / discover-feed | Full DS API category tree |
| `user1_aliexpress_crawl_categories` | `aliexpress-ds sync-categories` | **link-crawler** | calp-plus L1 seeds + multi-device claim state |
| `user1_aliexpress_us_product_urls` | **link-crawler** (+ ds discover-feed) | `aliexpress-ds fetch-es` | Product URL seeds |
| `user1_aliexpress_us_products` | `aliexpress-ds fetch-es --index-es` / product crawler | prepare-upload | Product details |

## Multi-device claim fields (`user1_aliexpress_crawl_categories`)

| Field | Meaning |
|-------|---------|
| `crawl_status` | `pending` / `claimed` / `done` / `failed` |
| `claimed_by` | `DEVICE_ID` (default: hostname) |
| `claimed_at` / `claim_expires_at` | lease window; expired claims can be stolen |
| `listing_total` | page/API advertised result count (best-effort) |
| `crawled_product_count` | product IDs seen while crawling this seed |
| `crawled_new_count` | newly saved URLs this run |
| `last_crawled_at` / `last_error` | completion / failure metadata |

`sync-categories` uses **partial update** so re-sync does not wipe claim progress.

## Workflow

```bash
# 1) Sync category tree + crawl seeds from DS API → ES
cd /home/sky/src/aliexpress-ds
uv run aliexpress-ds sync-categories \
  --yaml-out /home/sky/src/aliexpress-link-crawler/config/categories.priority.yaml

# 2) Point link-crawler at ES crawl seeds (.env)
#    ELASTICSEARCH_INDEX_CATEGORIES=user1_aliexpress_crawl_categories
#    DEVICE_ID=pc-a   # different on each machine

# 3) Crawl on machine A and B in parallel — each claims a batch for its workers
cd /home/sky/src/aliexpress-link-crawler
DEVICE_ID=pc-a ./run_crawl.sh
# other machine:
DEVICE_ID=pc-b ./run_crawl.sh

# 4) (Optional) Fill details via DS API
cd /home/sky/src/aliexpress-ds
uv run aliexpress-ds fetch-es -n 100 --index-es
```

## Notes

- Homepage calp tabs (`categoryTab=…`) and DS `category_id` are different taxonomies.
  `sync-categories` best-effort maps L1 names → `ds_category_id` for later `discover-feed -C`.
- Link-crawler falls back to YAML local queue if `CATEGORY_CLAIM_MODE=0` or ES categories unavailable.
- Listing pages sometimes expose a total count (API/`N results`); stored as `listing_total` when found. Calp infinite scroll does not always publish a reliable total.
- DS does **not** replace browser URL discovery for 200k scale; it supplies structure + optional feed discovery.
