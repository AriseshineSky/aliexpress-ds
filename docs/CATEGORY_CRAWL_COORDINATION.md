# Coordination: aliexpress-ds (category tree) ↔ aliexpress-link-crawler (URL scrape)
# Shared store: Elasticsearch

## Indices

| Index | Writer | Reader | Content |
|-------|--------|--------|---------|
| `user1_aliexpress_ds_categories` | `aliexpress-ds sync-categories` | ops / discover-feed | Full DS API category tree |
| `user1_aliexpress_crawl_categories` | `aliexpress-ds sync-categories` | **link-crawler** | calp-plus L1 seeds (Everymarket priority list) |
| `user1_aliexpress_us_product_urls` | **link-crawler** (+ ds discover-feed) | `aliexpress-ds fetch-es` | Product URL seeds |
| `user1_aliexpress_us_products` | `aliexpress-ds fetch-es --index-es` / product crawler | prepare-upload | Product details |

## Workflow

```bash
# 1) Sync category tree + crawl seeds from DS API → ES
cd /home/sky/src/aliexpress-ds
uv run aliexpress-ds sync-categories \
  --yaml-out /home/sky/src/aliexpress-link-crawler/config/categories.priority.yaml

# 2) Point link-crawler at ES crawl seeds (.env)
#    ELASTICSEARCH_INDEX_CATEGORIES=user1_aliexpress_crawl_categories

# 3) Crawl product list URLs → ES_URLS index
cd /home/sky/src/aliexpress-link-crawler
./run_crawl.sh   # or: .venv/bin/python alilj.py

# 4) (Optional) Fill details via DS API
cd /home/sky/src/aliexpress-ds
uv run aliexpress-ds fetch-es -n 100 --index-es
```

## Notes

- Homepage calp tabs (`categoryTab=…`) and DS `category_id` are different taxonomies.
  `sync-categories` best-effort maps L1 names → `ds_category_id` for later `discover-feed -C`.
- Link-crawler falls back to `config/categories.yaml` if ES is empty/unreachable.
- DS does **not** replace browser URL discovery for 200k scale; it supplies structure + optional feed discovery.
