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
# 只要 product_id（最快冲量；**没有** price/rating/sold）
uv run aliexpress-ds discover-itemids -f AEB_Droplo_BestsellersItems_20241016 -p 2000 --write-es
uv run aliexpress-ds discover-itemids-loop --target-unique 20000000 --write-es

# 后台冲 2000 万（默认走 itemids；也可 search / both）
./scripts/start_discover_20m.sh itemids
./scripts/start_discover_20m.sh search   # 用 reports 重提的 6 万关键词
```

| API | price | rating | sold_count | reviews |
|-----|-------|--------|------------|---------|
| `text.search`（discover-search） | ✅ | ✅ (`score`) | ✅ (`orders`) | ❌ |
| `recommend.feed.get` | ✅ | ✅ (`evaluate_rate`) | ✅ (`lastest_volume`) | 常缺 |
| `feed.itemids.get` | ❌ | ❌ | ❌ | ❌ |

```bash
# 关键词选品（官方 aliexpress.ds.text.search）— 指定类目目录，自动生成关键词海量拉 product_id
uv run aliexpress-ds discover-search -C 66 --pages 5 --max-keywords 40 --write-es
uv run aliexpress-ds discover-search -N "Beauty" --dry-run-keywords   # 只看生成的词
uv run aliexpress-ds discover-search -C 66 -k "nail art" -k "eyelash" --enqueue
# API 侧可用过滤（searchExtend；**没有** price/rating/sold 服务端过滤）：
uv run aliexpress-ds discover-search -C 66 -k "nail art" --choice --ship-from CN --write-es
uv run aliexpress-ds discover-search -C 66 -k "nail art" --free-ship-to US --seller-level GOLD

# 持续海量选品（关键词文件 + 深翻，目标 2000 万；返回并入库 price/rating/sold）
uv run aliexpress-ds discover-search-loop \
  --target-unique 20000000 \
  --keywords-file data/keywords/search_keywords_20m_plain.txt \
  --max-pages-per-keyword 250 --min-rating 0 --write-es
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
# 1) 每 2h 增量补队（不要 --reset-queue：会反复把同一批 unsaleable 旧 ID 排到队头）
#    已抓详情由 ES_PRODUCTS_INDEX 排除；永久失败打 ds_discard（unsaleable/not_found/…）
#    uv run aliexpress-ds sync-discard                 # Redis failed → ES urls
#    uv run aliexpress-ds sync-discard --from-jsonl data/ds_queue_products.jsonl
#    reviews>90 → 缺失/低评按 created_at DESC（新 URL 优先）
#    （systemd: aliexpress-ds-enqueue.timer → enqueue-es --no-reset-queue）
uv run aliexpress-ds enqueue-es --no-reset-queue
# 仅在策略变更时全量重建（保留 failed set）
uv run aliexpress-ds enqueue-es --reset-queue
uv run aliexpress-ds enqueue-es --reset-queue -n 500
uv run aliexpress-ds enqueue-es --dry-run
uv run aliexpress-ds enqueue-es --no-include-rest   # 只要质量过关
uv run aliexpress-ds queue-status

# 覆盖阈值 / 关闭过滤
uv run aliexpress-ds enqueue-es --reset-queue \
  --max-price 100 --min-rating 4.4 --min-reviews 300 --min-sold 500
uv run aliexpress-ds enqueue-es --no-quality-filter

# 2) 常驻消费
uv run aliexpress-ds queue-worker
```

`.env` 质量门槛（urls 索引字段）：

| 变量 | 默认 | 条件 |
|------|------|------|
| `ENQUEUE_REVIEWS_PRIORITY` | `1` | 启用三阶段流式优先级（关闭后使用下列旧质量门） |
| `ENQUEUE_REVIEWS_PRIORITY_THRESHOLD` | `90` | 先发 `reviews>90`，缺失其次，`reviews≤90` 最后 |
| `ENQUEUE_QUALITY_FILTER` | `1` | 启用过滤 |
| `ENQUEUE_MAX_PRICE` | `100` | `price < 100` |
| `ENQUEUE_MIN_RATING` | `4.4` | `rating ≥ 4.4` |
| `ENQUEUE_MIN_REVIEWS` | `300` | `reviews ≥ 300` |
| `ENQUEUE_MIN_SOLD_COUNT` | `500` | `sold_count ≥ 500` |

类目黑名单（默认开，跳过 **服装** / **成人用品**）：

| 变量 | 默认 | 说明 |
|------|------|------|
| `ENQUEUE_CATEGORY_BLACKLIST` | `1` | 启用类目黑名单 |
| `ENQUEUE_CATEGORY_BLACKLIST_FILE` | `config/category_blacklist.yaml` | 关键词列表 |
| `ENQUEUE_CATEGORY_BLACKLIST_KEYWORDS` | — | 逗号覆盖，如 `服装,成人用品,clothing` |

```bash
uv run aliexpress-ds enqueue-es --no-category-blacklist   # 临时关闭
```

### 一次性按类目补队 / 重刷（不传小时定时器）

小时定时器（mongo）跑的是默认逻辑（只补 products 索引里没有的 id）。想只针对某几个类目
**重刷详情/价格**（这些 id 早已在 products 索引里）时，用下面三个开关组合，一次性手动跑：

| 开关 | 作用 |
|------|------|
| `--categories/-C <root>`（可重复） | 只选 urls 索引 `category` 路径包含该子串的文档（大小写不敏感，`US / …` 与 `COM / …` 同时命中） |
| `--include-existing` | 绕过 products 索引去重（否则老 id 全被跳过） |
| `--no-seen` | 绕过 Redis seen 集合（否则已抓过的 id 全被跳过）；同一轮内仍按 product_id 去重 |

```bash
# 先 dry-run 看数量（十几分钟，纯读 ES）
uv run aliexpress-ds enqueue-es \
  -C "Automotive" -C "Tools & Home Improvement" -C "Sports & Outdoors" \
  --include-existing --no-seen --dry-run

# 确认后真灌（RPUSH，消费者 BRPOP 先取新队头）
uv run aliexpress-ds enqueue-es \
  -C "Automotive" -C "Tools & Home Improvement" -C "Sports & Outdoors" \
  --include-existing --no-seen
```

`ds_discard=true`（unsaleable / not_found / ship_prohibited）和 Redis failed 集合永久失败
的 id 仍会被跳过，不会重复重试。

### 授权与自动刷新 Token

1. **首次授权**（浏览器，一年内需在 refresh 过期前重做）：  
   https://aliexpress-oauth.onrender.com/oauth/authorize  
   成功后 token 写入 Upstash `REDIS_URL` 的 key `aliexpress:oauth:token`。
2. **多 AppKey**：token 按 `aliexpress:oauth:token:<app_key>` 存，每个 VPS 读自己 `.env` 里
   `ALIEXPRESS_APP_KEY` 对应的 key（主 App 同时写 legacy `aliexpress:oauth:token`）。
3. **本项目自动读/刷新**：每次 API 调用前若 `access_token` 将在 **1 小时内**过期，会调 `/auth/token/refresh` 并写回同一 Redis。
4. **手动**：`uv run aliexpress-ds refresh-token`（`--force` 强制刷新）。

> **token 缺失时 queue-worker 会启动即失败并退出**（systemd `Restart=always` 每 15s 重试），
> 不会 POP 任务。以前的行为是逐个 POP→报错→丢弃，会把整条队列悄悄抽干。
> 排查：`sudo journalctl -u aliexpress-ds-queue-worker -n 20` 看到
> `No access_token` 说明 Upstash 里没有该 app_key 的 token，重新授权后再
> `sudo systemctl start aliexpress-ds-queue-worker`。

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
| `aliexpress-ds-enqueue.timer` | 每 2 小时 **清空并重建** Redis 队列（质量门优先 + reviews DESC；默认只装在 mongo VPS） |
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
| **日配额**（Console 流量包） | `limited-by-app-access-count` / 日调用耗尽至北京时间 24:00 | 等到次日；Redis `apps.daily_limit`（或 env）作本地保护 |
| **App+API 频率** | `AppApiCallLimit`：*frequency of app access to the api* + `ban will last N seconds` | **睡 N+1 秒**；与是否用满日配额无关 |
| **API 总频率** | `ApiCallLimits` / `ban will last N seconds` | 同上 |

历史 AE 英文说明（三类 access count，域名在 alitrip，内容属 AliExpress Open Platform）：  
[API access count limitation](https://open.alitrip.com/docs/doc.htm?articleId=108426&docType=1)

**权威限额在 Redis** `aliexpress:oauth:apps`（按 AppKey）：

```bash
uv run aliexpress-ds app-limits-set -k 539674 -d 70000
uv run aliexpress-ds app-limits-show -k 539674
```

| 来源 | 日帽 | 间隔 |
|------|------|------|
| Redis `daily_limit=N` | **仅用于计算速率，不在本地停工** | `min(86400/N, 1s)` |
| Redis 无 `daily_limit` | 不设本地日帽 | **1.0s**（平台 ban 仍生效） |
| env 回退（无 Redis 注册） | 同样仅作速率依据 | 默认 **1.0s** |

查看：`uv run aliexpress-ds rate-status`（本地计数 + 最近一次平台错误原文）。

`IopClient.execute` 统一：按计算速率发请求 → 解析平台流控 → 等待官方返回的 ban 秒数 → 重试。本地计数永不触发停工；只有平台明确返回日配额耗尽才等待到北京时间次日。

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
