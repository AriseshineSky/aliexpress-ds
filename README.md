# AliExpress Dropshipping (Python + uv)

从商品 URL 调用 `aliexpress.ds.product.get`，解析标题、SKU 售价（`offer_sale_price`）和库存。

## 准备

```bash
cd /home/sky/src/aliexpress-ds
cp .env.example .env
```

编辑 `.env`：

| 变量 | 说明 |
|------|------|
| `ALIEXPRESS_APP_KEY` | App Console App Key |
| `ALIEXPRESS_APP_SECRET` | App Secret |
| `REDIS_URL` | **推荐**：Upstash Redis（与 `aliexpress-oauth` 同源，自动读 `aliexpress:oauth:token`） |
| `ALIEXPRESS_ACCESS_TOKEN` | 无 Redis 时的后备 Access Token |

```bash
uv sync
uv run aliexpress-ds config-check
```

## 发现商品（DS recommend feed，非关键词搜索）

当前 Dropshipping App **没有** Affiliate `product.query` 关键词搜索权限。可用的官方发现接口：

```bash
# 同步类目树 + 爬虫 calp 种子到 ES（与 aliexpress-link-crawler 协同）
uv run aliexpress-ds sync-categories \
  --yaml-out ../aliexpress-link-crawler/config/categories.priority.yaml

# 列出 feed / 预览类目
uv run aliexpress-ds feed-names
uv run aliexpress-ds categories

# 按 feed + 可选类目分页发现，写入 URL 索引
uv run aliexpress-ds discover-feed -f "DS bestseller" -C 66 --pages 20 --sort volumeDesc
```

协同说明见 `docs/CATEGORY_CRAWL_COORDINATION.md`。

自由关键词 / 全站类目爬取用 `aliexpress-link-crawler`（读 ES `user1_aliexpress_crawl_categories`）。

## 从 ES 差分拉取并写入 JSONL + ES 产品索引

urls 索引 `user1_aliexpress_us_product_urls` 里、且不在 `user1_aliexpress_us_products` 的链接：

```bash
# 调 DS API，写出 raw + StandardProduct（默认）
uv run aliexpress-ds fetch-es -o data/ds_products.jsonl

# 先小批量
uv run aliexpress-ds fetch-es -n 100 -o data/ds_products.jsonl
```

## 官方限流（Test 应用）

| 规则 | 来源 | 本项目默认 |
|------|------|-----------|
| **5,000 次/天/应用**（北京时间） | [Formal Test Environment](https://open.alitrip.com/docs/doc.htm?articleId=108105&docType=1) | `ALIEXPRESS_DAILY_LIMIT=5000` |
| App / API / 未上线应用 QPS 流控（`App Call Limited` / `ApiCallLimits`） | [API access count limitation](https://open.alitrip.com/docs/doc.htm?articleId=108426&docType=1) | `ALIEXPRESS_MIN_INTERVAL_SEC=1.0`（约 ≤1 QPS） |

- 日配额用尽到次日 00:00（GMT+8）才恢复；到达配额后 `fetch-es` 会停止。
- 遇到流控会按返回的 ban 秒数冷却并重试同一商品。
- 上线（Release）后日配额按应用类目提高；可在 Console 申请更多流量。

每行 JSONL 结构：

```json
{
  "ok": true,
  "product_id": "3256...",
  "url": "https://www.aliexpress.us/item/....html",
  "raw": { "...": "aliexpress.ds.product.get 原始返回" },
  "product": { "...": "StandardProduct 字段" }
}
```

`product` 对齐 [em_product.StandardProduct](https://github.com/AriseshineSky/product-validator/blob/d9cc852e58b73036a20db519dd9494bac5f9a363/src/em_product/product.py#L83)。

## 代码结构

- `iop_client.py` — IOP 签名（HMAC-SHA256）+ HTTP
- `url_parser.py` — URL → product_id
- `product.py` — `aliexpress.ds.product.get` 与字段归一化
- `cli.py` — Typer 命令行
