# Catalog metrics report

- generated_at: `2026-07-24T21:01:27.285457+00:00`
- host: `mongo`
- indices: `user1_aliexpress_us_products` (products), `user1_aliexpress_us_product_urls` (urls)

## 1. 总览

| 指标 | 数量 |
|------|------|
| products 全库 | **511204** |
| urls 全库 | **1070384** |
| bestsellers（urls 标记） | **30447** |
| bestsellers 已抓进 products | **12560** （41.25%） |
| bestsellers 尚未抓取 | **17887** |

## 2. 可导出数量（reviews 门槛）

以 **products 索引** `reviews` 字段为准。

| 范围 | 全库 products | bestsellers（已抓） |
|------|---------------|---------------------|
| reviews ≥ 100 | **89022** | **3730** |
| reviews ≥ 200 | **49288** | **2035** |
| reviews ≥ 300 | 32251 | 1375 |

全库占比：≥100 = 17.41%； ≥200 = 9.64%。

## 3. Bestsellers（已抓进 products）分布

### Rating

n=12549，mean=4.6985，min=0.0，max=5.0

| min | p25 | p50 | p75 | p90 | p99 | max |
|-----|-----|-----|-----|-----|-----|-----|
| 0.0 | 4.6 | 4.8 | 4.9 | 4.9 | 5.0 | 5.0 |

| 区间 | 数量 |
|------|------|
| <3 | 25 |
| 3–4 | 118 |
| 4–4.4 | 742 |
| 4.4–4.6 | 1456 |
| 4.6–4.8 | 3832 |
| 4.8–5 | 5366 |
| ≥5 | 1010 |

- `gte_4_4`: **11664**
- `gte_4_6`: **10208**

### Reviews

n=12549，mean=155.3208，min=0.0，max=12401.0

| min | p25 | p50 | p75 | p90 | p99 | max |
|-----|-----|-----|-----|-----|-----|-----|
| 0.0 | 24.0 | 50.0 | 122.2895 | 327.3683 | 1671.885 | 12401.0 |

| 区间 | 数量 |
|------|------|
| <1 | 18 |
| 1–10 | 875 |
| 10–50 | 5335 |
| 50–100 | 2591 |
| 100–200 | 1695 |
| 200–300 | 660 |
| 300–500 | 643 |
| 500–1000 | 468 |
| 1000–5000 | 236 |
| ≥5000 | 28 |

- `gte_100`: **3730**
- `gte_200`: **2035**
- `gte_300`: **1375**
- `gte_1000`: **264**

### Sold count

n=12549，mean=559.2658，min=12.0，max=10000.0

| min | p25 | p50 | p75 | p90 | p99 | max |
|-----|-----|-----|-----|-----|-----|-----|
| 12.0 | 91.3106 | 178.7804 | 600.0 | 1000.0 | 5000.0 | 10000.0 |

| 区间 | 数量 |
|------|------|
| <10 | 0 |
| 10–50 | 910 |
| 50–100 | 2645 |
| 100–500 | 5305 |
| 500–1000 | 1535 |
| 1000–5000 | 1887 |
| 5000–10000 | 184 |
| ≥10000 | 83 |

- `gte_500`: **3689**
- `gte_1000`: **2154**

### By feed（urls 标记）

```json
{
  "AEB_Droplo_BestsellersItems_20241016": 30447
}
```

## 4. 全库 products 分布

### 字段覆盖

| 字段 | 有值 | 缺失 | 覆盖率 |
|------|------|------|--------|
| rating | 501523 | 9681 | 98.11% |
| reviews | 501523 | 9681 | 98.11% |
| sold_count | 501522 | 9682 | 98.11% |

### Rating

n=501522，mean=4.4547，min=0.0，max=5.0

| min | p25 | p50 | p75 | p90 | p99 | max |
|-----|-----|-----|-----|-----|-----|-----|
| 0.0 | 4.6 | 4.803 | 5.0 | 5.0 | 5.0 | 5.0 |

| 区间 | 数量 |
|------|------|
| <3 | 35491 |
| 3–4 | 3687 |
| 4–4.4 | 28431 |
| 4.4–4.6 | 41044 |
| 4.6–4.8 | 82422 |
| 4.8–5 | 125718 |
| ≥5 | 184729 |

- `gte_4_4`: **433913**
- `gte_4_6`: **392869**

### Reviews

n=501522，mean=94.8511，min=0.0，max=38644.0

| min | p25 | p50 | p75 | p90 | p99 | max |
|-----|-----|-----|-----|-----|-----|-----|
| 0.0 | 2.0 | 13.0 | 60.2843 | 196.1218 | 1248.6862 | 38644.0 |

| 区间 | 数量 |
|------|------|
| <1 | 34676 |
| 1–10 | 190600 |
| 10–50 | 135186 |
| 50–100 | 52038 |
| 100–200 | 39734 |
| 200–300 | 17037 |
| 300–500 | 14780 |
| 500–1000 | 10569 |
| 1000–5000 | 6309 |
| ≥5000 | 593 |

- `gte_100`: **89022**
- `gte_200`: **49288**
- `gte_300`: **32251**
- `gte_1000`: **6902**

### Sold count

n=501521，mean=318.7214，min=0.0，max=50000.0

| min | p25 | p50 | p75 | p90 | p99 | max |
|-----|-----|-----|-----|-----|-----|-----|
| 0.0 | 7.0 | 43.7169 | 253.7449 | 800.0 | 5000.0 | 50000.0 |

| 区间 | 数量 |
|------|------|
| <10 | 143832 |
| 10–50 | 116253 |
| 50–100 | 51850 |
| 100–500 | 107596 |
| 500–1000 | 37522 |
| 1000–5000 | 38766 |
| 5000–10000 | 3804 |
| ≥10000 | 1898 |

- `gte_500`: **81990**
- `gte_1000`: **44468**

## 5. Blacklist category 产品数量

匹配规则：urls.`category` 对 `config/category_blacklist.yaml` 关键词 （与 enqueue-es 相同）。已抓产品 = urls 有 category 且 product_id 在 products 中。

| 指标 | 数量 |
|------|------|
| urls 命中 blacklist | **38050**（3.55% of urls w/ category） |
| 已抓 products 命中 blacklist | **3288**（0.58% of joined） |

### 按命中关键词（urls）

| keyword | urls | products |
|---------|------|----------|
| apparel | 23860 | 2339 |
| men's clothing | 13063 | 0 |
| clothing | 1081 | 948 |
| women's clothing | 46 | 1 |

### Top blacklisted categories（urls）

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

## 6. Category 产品数量分布

- urls 有 category: **1070536** （unique leaf categories: 1005）
- 已抓 products 经 urls.category 关联: **569929** （unique: 736）
- products.`categories` 字段有值（稀疏）: **18538**

### Top-level category（已抓 products）

| top-level | count |
|-----------|-------|
| Jewelry & Accessories | 51804 |
| Beauty & Health | 49538 |
| Automotive | 47555 |
| Toys & Games | 36692 |
| Home & Garden | 28832 |
| Cell Phones & Accessories | 28345 |
| Hair Extensions & Wigs | 25884 |
| Electronics | 25465 |
| Pet Supplies | 21069 |
| Consumer Electronics | 19551 |
| Patio, Lawn & Garden | 15780 |
| Phones & Telecommunications Accessories | 15549 |
| Sports & Entertainment | 15448 |
| COM / Automotive | 12269 |
| Lights & Lighting | 10547 |
| Phones & Telecommunications | 10455 |
| Tools & Home Improvement | 10190 |
| Mother & Kids | 8598 |
| COM / Patio, Lawn & Garden | 6862 |
| Sports & Outdoors | 6425 |
| Furniture | 6259 |
| Shoes | 6221 |
| COM / Toys & Games | 6132 |
| Baby & Maternity | 5787 |
| COM / Tools & Home Improvement | 5654 |
| COM / Jewelry & Accessories | 5543 |
| COM / Cell Phones & Accessories | 5515 |
| COM / Furniture | 5208 |
| COM / Baby & Maternity | 5195 |
| COM / Electronics | 4787 |

### Top-level category（urls 全量）

| top-level | count |
|-----------|-------|
| Toys & Games | 114240 |
| Cell Phones & Accessories | 89284 |
| Automotive | 87880 |
| Beauty & Health | 86549 |
| Jewelry & Accessories | 84511 |
| Hair Extensions & Wigs | 61202 |
| Pet Supplies | 59026 |
| Electronics | 49928 |
| Home & Garden | 32107 |
| Patio, Lawn & Garden | 31350 |
| Sports & Entertainment | 28785 |
| Tools & Home Improvement | 27536 |
| Mother & Kids | 21710 |
| Apparel Accessories | 21350 |
| Consumer Electronics | 20525 |
| Phones & Telecommunications Accessories | 16454 |
| COM / Automotive | 15186 |
| Men's Clothing | 13063 |
| Lights & Lighting | 13024 |
| Phones & Telecommunications | 12167 |
| COM / Bags & Luggage | 9306 |
| Sports & Outdoors | 8398 |
| COM / Patio, Lawn & Garden | 7689 |
| Office & School Supplies | 7601 |
| COM / Baby & Maternity | 7452 |
| Shoes | 7263 |
| COM / Cell Phones & Accessories | 6573 |
| COM / Tools & Home Improvement | 6474 |
| COM / Toys & Games | 6403 |
| Furniture | 6374 |

### Leaf category Top 30（已抓 products）

| category | count |
|----------|-------|
| US / Automotive | 29887 |
| Home & Garden | 27302 |
| Beauty & Health | 23673 |
| Jewelry & Accessories | 23297 |
| Consumer Electronics | 19409 |
| Phones & Telecommunications Accessories | 15549 |
| Sports & Entertainment | 15367 |
| US / Pet Supplies > Reptiles & Amphibians Supplies | 14497 |
| US / Hair Extensions & Wigs > Toupees, Toppers & Hair Systems | 14213 |
| US / Jewelry & Accessories > Handkerchiefs & Pocket Squares | 10879 |
| Lights & Lighting | 10547 |
| US / Electronics > Monitors & Screens | 10494 |
| COM / Automotive | 10379 |
| US / Beauty & Health > Dental Equipment | 9996 |
| Phones & Telecommunications | 9617 |
| US / Toys & Games | 8564 |
| US / Automotive > Jacks & Lifting | 8391 |
| US / Tools & Home Improvement | 8327 |
| US / Cell Phones & Accessories > Custom Phone Cases | 8113 |
| US / Jewelry & Accessories > Keychains & Lanyards | 8050 |
| US / Toys & Games > Inflatable & Bouncy Toys | 7431 |
| US / Beauty & Health > Toilet Paper | 7074 |
| US / Patio, Lawn & Garden > Cookware & Bakeware | 7057 |
| US / Patio, Lawn & Garden | 6815 |
| US / Electronics > Atomizers & Coils | 6207 |
| COM / Patio, Lawn & Garden | 6143 |
| COM / Toys & Games | 6101 |
| US / Furniture | 6028 |
| Mother & Kids | 5972 |
| US / Toys & Games > Fashion & Collectible Dolls | 5908 |

## 7. 结论（简要）

- Bestsellers 已抓评分中位 **4.8**，reviews 中位 **50.0**，sold 中位 **178.7804**。
- 全库 products reviews 中位 **13.0**，≥100 可导出 **89022**，≥200 可导出 **49288**。
- Blacklist 已抓产品 **3288** / urls **38050**。
