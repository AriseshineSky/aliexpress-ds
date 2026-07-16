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

## 按 product_id 取商品信息 / 价格

```bash
# 单个：URL 或纯数字 product_id
uv run aliexpress-ds get 1005004506189269
uv run aliexpress-ds get 'https://www.aliexpress.us/item/1005004506189269.html'
```

核心代码：`src/aliexpress_ds/product.py`（`aliexpress.ds.product.get`）→ `standard_mapper.py` → ES。

## 从 ES 差分拉取并写入 JSONL + ES 产品索引

urls 索引 `user1_aliexpress_us_product_urls` 里、且不在 `user1_aliexpress_us_products` 的链接：

```bash
# 调 DS API，写出 raw + StandardProduct（默认）
uv run aliexpress-ds fetch-es -o data/ds_products.jsonl

# 先小批量
uv run aliexpress-ds fetch-es -n 100 -o data/ds_products.jsonl
```

## Redis 队列：灌队 + 消费写 ES

`.env` 里配置任务队列（与 OAuth 的 `REDIS_URL` 分开）：

```env
# OAuth token（Upstash，与 aliexpress-oauth 同源）
REDIS_URL=rediss://default:PASSWORD@xxxxx.upstash.io:6379

# 任务队列（GCP Redis）
REDIS_QUEUE_URL=redis://:password@34.133.1.247:6379/0
REDIS_QUEUE_KEY=aliexpress-ds:products
REDIS_QUEUE_SEEN_KEY=aliexpress-ds:products:seen
```

```bash
# 1) 从 ES urls 入队（默认带质量过滤 + 排除已在 products 索引）
uv run aliexpress-ds enqueue-es
uv run aliexpress-ds enqueue-es -n 500
uv run aliexpress-ds enqueue-es --dry-run
uv run aliexpress-ds queue-status

# 覆盖阈值 / 关闭过滤
uv run aliexpress-ds enqueue-es \
  --max-price 100 --min-rating 4.4 --min-reviews 1000 --min-sold 1000
uv run aliexpress-ds enqueue-es --no-quality-filter

# 2) 常驻消费
uv run aliexpress-ds queue-worker
```

`.env` 质量门槛（urls 索引字段）：

| 变量 | 默认 | 条件 |
|------|------|------|
| `ENQUEUE_QUALITY_FILTER` | `1` | 启用过滤 |
| `ENQUEUE_MAX_PRICE` | `100` | `price < 100` |
| `ENQUEUE_MIN_RATING` | `4.4` | `rating ≥ 4.4` |
| `ENQUEUE_MIN_REVIEWS` | `1000` | `reviews ≥ 1000` |
| `ENQUEUE_MIN_SOLD_COUNT` | `1000` | `sold_count ≥ 1000` |

类目黑名单（默认开，跳过 **服装** / **成人用品**）：

| 变量 | 默认 | 说明 |
|------|------|------|
| `ENQUEUE_CATEGORY_BLACKLIST` | `1` | 启用类目黑名单 |
| `ENQUEUE_CATEGORY_BLACKLIST_FILE` | `config/category_blacklist.yaml` | 关键词列表 |
| `ENQUEUE_CATEGORY_BLACKLIST_KEYWORDS` | — | 逗号覆盖，如 `服装,成人用品,clothing` |

```bash
uv run aliexpress-ds enqueue-es --no-category-blacklist   # 临时关闭
```

### 授权与自动刷新 Token

1. **首次授权**（浏览器，一年内需在 refresh 过期前重做）：  
   https://aliexpress-oauth.onrender.com/oauth/authorize  
   成功后 token 写入 Upstash `REDIS_URL` 的 key `aliexpress:oauth:token`。
2. **本项目自动读/刷新**：每次 API 调用前若 `access_token` 将在 **1 小时内**过期，会调 `/auth/token/refresh` 并写回同一 Redis。
3. **手动**：`uv run aliexpress-ds refresh-token`（`--force` 强制刷新）。

### 部署到 VPS（Admin@34.172.204.102）

```bash
./scripts/deploy_vps.sh
```

安装目录：`/home/Admin/aliexpress-ds`  
systemd：

| Unit | 作用 |
|------|------|
| `aliexpress-ds-queue-worker.service` | 常驻监听 Redis 队列 |
| `aliexpress-ds-token-refresh.timer` | 每小时确保 token 新鲜 |

```bash
sudo systemctl status aliexpress-ds-queue-worker
sudo journalctl -u aliexpress-ds-queue-worker -f
sudo systemctl list-timers 'aliexpress-ds*'
```

## 官方限流（Open Platform）

官方文档不公布每个 DS API 的固定 QPS，而是三类限制 + response 退避：

| 限制 | 子错误码 / 表现 | 处理 |
|------|-----------------|------|
| **AppKey 日配额**（北京时间） | `accesscontrol.limited-by-app-access-count`；Test=5000/天，上线后看 Console | 等到次日 00:00 GMT+8；本地用 `ALIEXPRESS_DAILY_LIMIT` 对齐 Console |
| **API 级 QPS**（全平台） | `accesscontrol.limited-by-api-access-count` / `ApiCallLimits`；`ban will last N seconds` | **睡 N+1 秒后重试** |
| **App+API 频率**（未上线常见） | `accesscontrol.limited-by-app-api-access-count`；错误码 **7** `App Call Limited` | 同上；上线后通常放宽 |

文档：
- [API access count limitation](https://open.alitrip.com/docs/doc.htm?articleId=108426&docType=1)
- [App Call Limited (code 7)](https://developer.alibaba.com/docs/doc.htm?articleId=108869&docType=1)
- [Environments (Test 5k / Online by category)](https://open.fliggy.com/docs/doc.htm?articleId=108101&docType=1)

本项目默认：

| 变量 | 默认 | 说明 |
|------|------|------|
| `ALIEXPRESS_DAILY_LIMIT` | `5000` | 对齐 Test；上线后改成 Console 流量包（如 `100000`） |
| `ALIEXPRESS_MIN_INTERVAL_SEC` | `0.5` | 主动约 ≤2 QPS；遇流控会自适应拉长间隔 |
| `ALIEXPRESS_MAX_RETRIES` | `6` | 每次 API：按 ban 秒数退避重试；传输错误指数退避 |

`IopClient.execute` 统一：限速 → 发请求 → 解析流控 → 等待官方 ban 秒数 → 重试；日配额耗尽抛 `DailyQuotaExhausted` 并停工。

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
