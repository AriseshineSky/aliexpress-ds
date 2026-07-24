# Bestsellers 指标完整报告（已剔除服装 / 成人用品）

- generated_at: `2026-07-24T21:15:28.471743+00:00`
- host: `mongo`
- products index: `user1_aliexpress_us_products`
- urls index: `user1_aliexpress_us_product_urls`
- blacklist: `config/category_blacklist.yaml` （clothing / adult，与 enqueue-es 相同）

## 0. 先回答：≥100 / ≥200 是否已去服装和成人？

**上一版报告里的 89,022 / 49,288 是全库、未剔 blacklist。**

本报告给出 **剔除服装 + 成人用品之后** 的可导出数量：

| 门槛 | 全库 products | bestsellers 已抓 |
|------|--------------------------------|-------------------------------------|
| reviews ≥ 100 | **88312**（17.31%） | **3505** |
| reviews ≥ 200 | **49027**（9.61%） | **1954** |


全库因 blacklist 排除的有 reviews 的产品：**3208**；bestsellers 已抓因 blacklist 排除：**1081**。

## 1. 总览

| 指标 | 数量 |
|------|------|
| products 全库 | **510270** |
| bestsellers（urls 标记） | **30447** |
| bestsellers 已抓进 products | **12652** （41.55%） |
| bestsellers 已抓且 | **11704** |

## 2. 百分位怎么读

把商品按该字段从小到大排：

- **p50** = 中位数（一半商品 ≥ 这个值）
- **p90** = 只有顶部约 10% 超过这个值
- **p25 / p75** = 下四分位 / 上四分位

### Bestsellers 已抓百分位

| 字段 | p25 | p50 | p75 | p90 | 含义 |
|------|-----|-----|-----|-----|------|
| rating | 4.6 | 4.7 | 4.9 | 4.9 | 一半 ≥4.7，整体很高 |
| reviews | 24.0 | 50.0 | 125.0 | 343.0 | 一半评论不到 50.0；只有约 10% >343.0 |
| sold_count | 90.0 | 174.0 | 600.0 | 1000.0 | 中位销量 174.0；约 10% >1000.0 |

样本 n：rating=11686，reviews=11686，sold=11686；mean rating=4.69，reviews=160.03，sold=564.91。

## 3. 「≥ 某阈值」怎么读（单条件，已剔 blacklist）

| 条件 | 数量 | 约占已抓 bestsellers（剔后） | 松紧 |
|------|------|------------------------------|------|
| rating ≥ 4.4 | **10831** | ~92.54% | 很松 |
| rating ≥ 4.6 | **9430** | ~80.57% | 很松 |
| sold ≥ 500 | **3449** | ~29.47% | 中等 |
| sold ≥ 1000 | **2038** | ~17.41% | 中等 |
| reviews ≥ 100 | **3505** | ~29.95% | 中等 |
| reviews ≥ 200 | **1954** | ~16.7% | 中等 |
| reviews ≥ 300 | **1348** | ~11.52% | 偏紧 |
| reviews ≥ 1000 | **253** | ~2.16% | 极紧 |

## 4. Blacklist 剔除明细

| 指标 | 数量 |
|------|------|
| urls 命中 blacklist 的 product_id（去重） | **37993** |
| bestsellers 命中 blacklist | **1081** |
| bestsellers 已抓且命中 | **1081** |

### 按命中关键词（urls 行计数，非去重）

| keyword | urls hits |
|---------|-----------|
| apparel | 23860 |
| men's clothing | 13063 |
| clothing | 1081 |
| women's clothing | 46 |

### Top blacklisted categories

| category | count |
|----------|-------|
| Apparel Accessories | 21350 |
| Men's Clothing | 13000 |
| US / Apparel & Accessories | 2361 |
| US / Mother & Kids > Children's Clothing | 662 |
| US / Mother & Kids > Baby Clothing | 419 |
| COM / Apparel & Accessories > Women > Dresses | 75 |
| US / Apparel & Accessories > Women > Dresses | 74 |
| US / Women's Clothing > Lingerie Sets | 46 |
| US / Men's Clothing > Sneakers & Casuals | 40 |
| US / Men's Clothing > Casual & Cargo Shorts | 23 |

## 5. 结论

- 导出门槛请看 **已剔 blacklist**：全库 ≥100 → **88312**，≥200 → **49027**；bestsellers ≥100 → **3505**，≥200 → **1954**。
- Bestsellers（剔后）评分很高（p50=4.7），但 reviews 仍偏紧（p50=50.0，p90=343.0）；reviews 是最紧的单条件过滤。
- sold 中位 174.0；sold≥500 约占 29.47%。
