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

# 列出 feed / 预览类目（bestsellers 池）
uv run aliexpress-ds feed-names --bestsellers-only
uv run aliexpress-ds categories

# 单 feed + 可选类目翻页 → ES urls（补漏）
uv run aliexpress-ds discover-feed \
  -f AEB_Droplo_BestsellersItems_20241016 -C 66 -p 50 --sort volumeDesc

# 多 feed × 优先类目循环翻页（推荐，可断点续跑）
uv run aliexpress-ds discover-feed-loop --pages-per-job 100 --write-es
```

`recommend.feed`：平台精选池（如 Droplo bestsellers / `DS_*_bestsellers`），用 `category_id` 收窄后再 `page_no` 翻页，直到 `is_finished`。用来补 keyword search / 爬虫没扫到的高销量商品。

```bash
# 关键词选品（官方 aliexpress.ds.text.search）— 指定类目目录，自动生成关键词海量拉 product_id
uv run aliexpress-ds discover-search -C 66 --pages 5 --max-keywords 40 --write-es
uv run aliexpress-ds discover-search -N "Beauty" --dry-run-keywords   # 只看生成的词
uv run aliexpress-ds discover-search -C 66 -k "nail art" -k "eyelash" --enqueue
# API 侧可用过滤（searchExtend；**没有** price/rating/sold）：
uv run aliexpress-ds discover-search -C 66 -k "nail art" --choice --ship-from CN --write-es
uv run aliexpress-ds discover-search -C 66 -k "nail art" --free-ship-to US --seller-level GOLD

# 持续海量选品（多 L1 轮询，分页直到空，目标 100 万 unique，2s/次 + 流控退避）
uv run aliexpress-ds discover-search-loop \
  --target-unique 1000000 --page-size 50 --min-interval 2.0 --min-rating 4.0 \
  --max-keywords 200 --max-leaves 150 --write-es
```

`discover-search` / `discover-search-loop` 写入 `ES_URLS_INDEX`（bulk **update + doc_as_upsert**）。本地默认丢弃 `rating < 4.0`（`--min-rating 4.0`；缺评分保留；`--min-rating 0` 关闭）。质量过滤（price/reviews/sold）仍用后续 `enqueue-es --quality-filter`。

协同说明见 `docs/CATEGORY_CRAWL_COORDINATION.md`。

自由关键词 / 全站类目网页爬取仍可用 `aliexpress-link-crawler`（读 ES `user1_aliexpress_crawl_categories`）。

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
#    质量过关的在前；缺 rating/reviews/sold 的自动追加到队列后面
uv run aliexpress-ds enqueue-es
uv run aliexpress-ds enqueue-es -n 500
uv run aliexpress-ds enqueue-es --dry-run
uv run aliexpress-ds enqueue-es --no-include-missing-fields   # 只要质量过关
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

### 部署到 VPS（Admin@34.172.204.102 / mongo）

默认用 **git pull**（保留远端 `.env` / `.venv` / `data/`）：

```bash
./scripts/deploy_vps.sh
# 或
DEPLOY_HOST=Admin@35.202.167.107 ./scripts/deploy_vps.sh

# 只更新代码、不重启 worker：
SKIP_START=1 ./scripts/deploy_vps.sh

# 仍可用 rsync：
DEPLOY_MODE=rsync ./scripts/deploy_vps.sh
```

在 VPS 上手动更新：

```bash
cd /home/Admin/aliexpress-ds
git fetch origin && git reset --hard origin/main
uv sync
sudo systemctl restart aliexpress-ds-queue-worker
```

安装目录：`/home/Admin/aliexpress-ds`  
systemd：

| Unit | 作用 |
|------|------|
| `aliexpress-ds-queue-worker.service` | 常驻监听 Redis 队列 |
| `aliexpress-ds-token-refresh.timer` | 每小时确保 token 新鲜 |
| `aliexpress-ds-enqueue.timer` | 每 2 小时从 ES urls 补灌 Redis 队列（默认只装在 mongo VPS） |
| `aliexpress-ds-bestsellers.timer` | **每天**同步 bestsellers → 标记 → 缺详情优先入队 → 分布报告 |

```bash
sudo systemctl status aliexpress-ds-queue-worker
sudo journalctl -u aliexpress-ds-queue-worker -f
sudo journalctl -u aliexpress-ds-enqueue -n 50
sudo journalctl -u aliexpress-ds-bestsellers -n 100
sudo systemctl list-timers 'aliexpress-ds*'
# 手动跑一天任务（先 dry-run）
cd /home/Admin/aliexpress-ds && uv run aliexpress-ds bestsellers-daily --dry-run
uv run aliexpress-ds bestsellers-daily
```

报告输出：`reports/bestsellers_YYYYMMDD.md` / `.json`（以及 `bestsellers_latest.*`）。

## 官方限流（AliExpress Open Service）

控制台（你的 App）：[openservice.aliexpress.com App Console](https://openservice.aliexpress.com/app/index.htm)  
文档入口：[Documentation](https://openservice.aliexpress.com/doc/doc.htm) → **DropShippers API Developer**  
工单/支持：[Contact us](https://openservice.aliexpress.com/support/index.htm)

**不要用淘宝开放平台**（`developer.alibaba.com` 的「管理证书 / 流量包」FAQ，如 articleId=101164）——那是 TOP 商家/服务市场应用规则，不是速卖通 Dropshipping。

日调用量（如 10万/天）以 **App Console 概览 / 证书** 为准。成功 API 响应**不返回**剩余配额。

平台对调用还有频率类限制（与日配额独立）。`aliexpress.ds.product.get` 官方社区说明：

- [Current limiting when calling aliexpress.ds.product.get](https://openservice.aliexpress.com/dada/community/index.htm?#/article-detail/1901) — 遇流控后 sleep 再重试

本项目见到的真实错误多为：

| 限制 | 表现 | 处理 |
|------|------|------|
| **日配额**（Console 流量包） | `limited-by-app-access-count` / 日调用耗尽至北京时间 24:00 | 等到次日；本地 `ALIEXPRESS_DAILY_LIMIT` 仅作客户端保护，应对齐 Console |
| **App+API 频率** | `AppApiCallLimit`：*frequency of app access to the api* + `ban will last N seconds` | **睡 N+1 秒**；与是否用满 10万/天无关 |
| **API 总频率** | `ApiCallLimits` / `ban will last N seconds` | 同上 |

历史 AE 英文说明（三类 access count，域名在 alitrip，内容属 AliExpress Open Platform）：  
[API access count limitation](https://open.alitrip.com/docs/doc.htm?articleId=108426&docType=1)

本项目默认：

| 变量 | 默认 | 说明 |
|------|------|------|
| `ALIEXPRESS_DAILY_LIMIT` | `100000` | **本地**日帽，对齐 Console（如 10万）；`0`=不本地掐断 |
| `ALIEXPRESS_MIN_INTERVAL_SEC` | `1.0` | 主动 pacing，降低 `AppApiCallLimit` |
| `ALIEXPRESS_MAX_RETRIES` | `6` | 按 ban 秒数退避；传输错误指数退避 |

查看：`uv run aliexpress-ds rate-status`（本地计数 + 最近一次平台错误原文）。

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
