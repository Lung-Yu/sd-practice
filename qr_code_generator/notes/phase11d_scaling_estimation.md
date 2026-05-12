# Phase 11d — 極限規模估算：1M → 1B DAU（2026-05-12）

## 文件說明

本文件以 Phase 1–11c 的實測數據為基礎，推算 QR Code Generator 在不同 DAU 規模下的
系統需求、架構演進、瓶頸轉移，以及成本量級。

所有推算分為三類：
- **實測（✓）**：本專案直接量測的數字
- **估算（≈）**：基於實測外推，假設線性擴展
- **假設（*）**：需要根據實際使用行為調整的參數

---

## 一、流量模型假設

### 使用者行為基準（*）

| 行為 | 假設值 | 說明 |
|------|--------|------|
| 每 DAU 每日掃碼次數 | 5 次 | 平均每人每天掃 5 個 QR code |
| 每 DAU 每日建立次數 | 0.01 次 | 100 人中有 1 人每天建立 QR code |
| 峰值係數（peak factor）| 3× | 高峰期 QPS = 平均 QPS × 3 |
| 高峰期持續時間 | 2 小時 | 用於 DB/Redis 容量估算 |
| 快取命中率（CDN）| 95–99% | 熱門 QR code 被重複掃描 |
| 快取命中率（Redis）| 99% | 非 CDN 路徑的 Redis 命中率 |

### 核心公式

```
redirect_QPS_avg = DAU × scans_per_day / 86400
redirect_QPS_peak = redirect_QPS_avg × peak_factor

create_QPS_avg = DAU × creates_per_day / 86400
create_QPS_peak = create_QPS_avg × peak_factor

帶入預設值：
  redirect_QPS_peak = DAU × 5 × 3 / 86400  =  DAU × 0.0001736
  create_QPS_peak   = DAU × 0.01 × 3 / 86400 = DAU × 3.47 × 10⁻⁷
```

---

## 二、各 DAU 規模的流量需求

| DAU | redirect 峰值 QPS | create 峰值 QPS | 日 create 筆數 |
|-----|-----------------|----------------|--------------|
| 100K | 17 req/s | 0.03 req/s | 1,000 |
| 1M | **174 req/s** | 0.35 req/s | 10,000 |
| 10M | **1,736 req/s** | 3.5 req/s | 100,000 |
| 50M | **8,681 req/s** | 17.4 req/s | 500,000 |
| 100M | **17,361 req/s** | 34.7 req/s | 1,000,000 |
| 500M | **86,806 req/s** | 174 req/s | 5,000,000 |
| 1B | **173,611 req/s** | 347 req/s | 10,000,000 |

---

## 三、已知系統容量（實測基準）

| 組件 | 容量 | 條件 | Phase |
|------|------|------|-------|
| 單 site（app + nginx + Redis）| ~2,600 req/s（redirect）| 1 Podman VM, 4 app × 4 workers | 11a ✓ |
| 單 site + Varnish CDN | ~5,100 req/s（redirect peak）| 99%+ cache hit rate | 11c ✓ |
| create throughput | ~630 req/s | 1 site, PgBouncer pool 40 | 9 ✓ |
| Redis GET throughput | > 100,000 req/s | 單節點上限（理論）| — |
| PgBouncer pool | 40 真實 PG 連線 | DEFAULT_POOL_SIZE=40 | 9 ✓ |
| PostgreSQL write（sync=off）| ~630 INSERT/s | synchronous_commit=off | 8 ✓ |
| Varnish cache（單節點）| ~5,100 req/s（HIT）| 256MB, 500 tokens | 11c ✓ |

**真實多主機線性擴展（≈）：**

```
N sites × 2,600 req/s = redirect capacity（app path）
N sites × 5,100 req/s = redirect capacity（CDN + app path）
N sites × 630 req/s = create capacity
```

---

## 四、各 DAU 規模的架構配置

### Tier 1：100K – 1M DAU（17 – 174 req/s redirect peak）

**結論：當前 Phase 11c 架構已綽綽有餘**

```
Varnish（256MB cache）
    │
nginx（1 台）
    │
app1/app2（4 workers 各）
    │
Redis → PgBouncer → PostgreSQL
```

| 指標 | 需求 | 當前容量 | 餘裕 |
|------|------|---------|------|
| redirect QPS（peak）| 174 req/s | 5,100 req/s | 29× |
| create QPS（peak）| 0.35 req/s | 630 req/s | 1,800× |
| 日儲存增量 | 10,000 × 500B = 5MB | 無限制（磁碟）| — |
| Redis 記憶體 | 10K 活躍 token × 256B = 2.5MB | 256MB | 100× |

**無需任何架構變更，單台主機即可處理。**

---

### Tier 2：10M DAU（1,736 req/s redirect peak）

**結論：單 site + Varnish 仍可應付；需關注 Redis 記憶體**

```
Varnish（1GB cache）
    │
nginx
    │
app1~app4（各 4 workers）
    │
Redis（1 instance）→ PgBouncer → PostgreSQL Primary + Replica
```

| 指標 | 需求 | 配置 | 說明 |
|------|------|------|------|
| redirect QPS（peak）| 1,736 req/s | 5,100 req/s（Varnish）| 2.9× 餘裕 |
| create QPS（peak）| 3.5 req/s | 630 req/s | 180× 餘裕 |
| 日儲存增量 | 100K × 500B = 50MB | — | 年增 18GB |
| Redis 活躍 token | 估計 1M | 256MB（≈ 256B/token）| 需升規 Redis 至 1GB |
| DB read（analytics）| 低 | Read Replica 承接 | ✓ |

**關鍵動作：Redis 從 256MB 升至 1GB；Varnish cache 從 256MB 升至 1GB。**

---

### Tier 3：50M DAU（8,681 req/s redirect peak）

**結論：需要 2 個真實主機 site + CDN；單機達不到**

```
Global LB（DNS 輪詢 or Anycast）
    ├── Site 1（Host A）：Varnish + nginx + app1/app2
    └── Site 2（Host B）：Varnish + nginx + app3/app4
                │
        共用 Redis Cluster（3 主 3 從）
        共用 PgBouncer Cluster → PostgreSQL Primary + 2 Replicas
```

| 指標 | 需求 | 配置 | 說明 |
|------|------|------|------|
| redirect QPS（peak）| 8,681 req/s | 2 × 5,100 = 10,200 req/s | 1.17× 餘裕 |
| create QPS（peak）| 17.4 req/s | 2 × 630 = 1,260 req/s | 72× 餘裕 |
| Redis 記憶體（活躍 token）| 5M token × 256B = 1.28GB | Redis Cluster 3 節點 | ✓ |
| DB storage（累積）| 500K/日 × 365 × 500B ≈ 91GB/年 | SSD + 備份 | 需規劃 |
| DB 連線（create）| 17.4 × 5ms / worker | PgBouncer pool 40×2 | 充裕 |

**關鍵決策：Redis 改為 Cluster（水平分片），避免單點記憶體瓶頸。**

---

### Tier 4：100M DAU（17,361 req/s redirect peak）

**結論：4 sites + CDN；DB write 仍輕鬆，DB storage 成為長期規劃重點**

```
Global LB（Anycast）
    ├── Region A：Varnish edge + Site 1 + Site 2
    └── Region B：Varnish edge + Site 3 + Site 4
                    │
        Redis Cluster（6 主 6 從，跨 Region）
        PgBouncer → PostgreSQL Primary（1 台）+ Replicas（3 台）
```

| 指標 | 需求 | 配置 | 說明 |
|------|------|------|------|
| redirect QPS（peak）| 17,361 req/s | 4 × 5,100 = 20,400 req/s | 1.17× 餘裕 |
| create QPS（peak）| 34.7 req/s | 4 × 630 = 2,520 req/s | 72× 餘裕 |
| Redis 活躍 token | 10M × 256B = 2.56GB | Cluster 分片（各節點 ~1GB）| ✓ |
| DB storage | 1M/日 × 365 × 500B = 182GB/年 | 需 SSD RAID + 定期 archive | 3 年後 ~550GB |
| DB connections | 34.7 req/s × 5ms = 0.17 並發 | PgBouncer pool 160 | 極度充裕 |
| PgBouncer → PG 連線 | < 5 個真實連線 | pool=40（每 site）| 充裕 |

**關鍵洞察：在 100M DAU 規模，create 的 DB 負載（34.7 req/s）比 redirect 輕幾百倍。**
**真正的長期成本在 DB 儲存，不在 compute。**

**CDN hit rate 的影響：**

```
假設 CDN hit_rate = 95%（5% MISS 打到 Redis/app）：
  CDN HIT 流量 = 17,361 × 95% = 16,493 req/s → 由 4 個 Varnish 邊緣節點承接
  MISS 流量    = 17,361 × 5%  =    868 req/s → 由 4 個 app site 承接（各 217 req/s，遠低於 2,600 上限）

若 CDN hit_rate 降至 80%（URL 多樣性高）：
  MISS 流量    = 17,361 × 20% = 3,472 req/s → 仍在 4 sites（20,400 req/s）範圍內 ✓
```

---

### Tier 5：500M DAU（86,806 req/s redirect peak）

**結論：需要地理分散部署 + 多 Region CDN**

```
GeoDNS（依地理路由到最近 Region）
    ├── Region 亞太：4 sites + CDN edge cluster → 20,400 req/s
    ├── Region 歐洲：4 sites + CDN edge cluster → 20,400 req/s
    ├── Region 美東：4 sites + CDN edge cluster → 20,400 req/s
    └── Region 美西：4 sites + CDN edge cluster → 20,400 req/s
                                │
                  全球 DB 策略（見下方）
```

| 指標 | 需求 | 配置 |
|------|------|------|
| redirect QPS（global）| 86,806 req/s | 4 Regions × 20,400 = 81,600 req/s（需微調，+2 site）|
| create QPS（global）| 174 req/s | 4 Regions × 2,520 = 10,080 req/s（極度充裕）|
| CDN MISS（hit=97%）| 86,806 × 3% = 2,604 req/s | 全球分散，各 Region ~651 req/s |
| DB storage | 5M/日 × 500B × 365 = 912GB/年 | 分 Region 分片 or 中央 DB + 快取 |

**全球 DB 策略（取捨）：**

| 方案 | 優點 | 缺點 |
|------|------|------|
| 中央 DB（單 Primary）| 一致性強 | 跨 Region 寫入延遲 50–150ms |
| 多 Region Primary（分片）| write 低延遲 | token 空間分片，路由複雜 |
| CRDT / eventual consistency | 最低延遲 | QR code 建立有短暫衝突風險 |

**建議：read 走 local Replica + CDN；write 走中央 Primary（create QPS 極低，跨 Region 延遲可接受）。**

---

### Tier 6：1B DAU（173,611 req/s redirect peak）

**結論：CDN 是主力；app 層承接 MISS 流量**

```
全球 Anycast CDN（CloudFront / Cloudflare）
    │ 99%+ HIT（173,437 req/s 由 CDN 邊緣服務）
    │ < 1% MISS（1,736 req/s 打到 Origin）
    │
Origin（多 Region）
    ├── Region 亞太：2 sites → 5,200 req/s 容量（MISS 流量 ~500 req/s）
    ├── Region 歐洲：2 sites → 5,200 req/s 容量
    ├── Region 美東：2 sites → 5,200 req/s 容量
    └── Region 美西：2 sites → 5,200 req/s 容量
                    │
        Global Redis Cluster（hot key replication + Consistent Hashing）
        Global DB：分 Region 分片 Primary + 本地 Replica
```

| 指標 | 需求 | 備注 |
|------|------|------|
| CDN 承接 redirect（99% HIT）| 171,875 req/s | CDN 邊緣節點（全球分散）|
| Origin MISS 流量 | 1,736 req/s | 8 sites × 2,600 = 20,800 req/s 容量（11.9× 餘裕）|
| create QPS | 347 req/s | 8 sites × 630 = 5,040 req/s（14.5× 餘裕）|
| DB write（日）| 10M creates/日 | 年增 1.83TB（需規劃 cold storage tiering）|
| CDN 快取大小 | 活躍 token × 256B | 假設 10M 活躍 = 2.56GB per edge node |

**在 1B DAU 規模，app 層（Python + Redis）幾乎閒置；真正的工程挑戰在：**
1. **CDN 的 cache invalidation**（token expire / URL update 的 PURGE 全球同步）
2. **DB storage tiering**（10B+ records，冷資料需遷移至 object storage）
3. **Redis Hot Key**（最熱的 QR code 可能在同一分片上 → consistent hashing + local replication）

---

## 五、規模瓶頸轉移一覽

| DAU 規模 | 主要瓶頸 | 解決手段 |
|---------|---------|---------|
| < 1M | 無瓶頸（單機綽綽有餘）| — |
| 1M–10M | Redis 記憶體 | 升規 Redis instance（1–4GB）|
| 10M–50M | 單機 redirect ceiling | 加第 2 個 app host（真實多主機）|
| 50M–100M | 跨 Region 延遲（create）| GeoDNS + 各 Region 獨立 Varnish + Redis Cluster |
| 100M–500M | DB storage 增長 | 分片 + cold storage tiering；DB 並不是 QPS 瓶頸 |
| 500M–1B | CDN cache invalidation / hot key | PURGE 全球同步機制；Redis local replica for hot keys |

**關鍵洞察：QR code redirect 的 DB write 負載在任何規模下都不是瓶頸**。
即使是 1B DAU（347 create/s peak），8 sites 的 create 容量（5,040/s）有 14× 餘裕。
真正的挑戰在儲存、CDN 一致性、以及 hot key。

---

## 六、成本量級估算（雲端，以 AWS 為例）

| DAU | 主要組件 | 月費估算（USD）|
|-----|---------|-------------|
| 100K | 1 VM（4 vCPU）+ RDS t3.medium + ElastiCache t3.micro | ~$150 |
| 1M | 1 VM（8 vCPU）+ RDS m6g.large + ElastiCache m6g.large | ~$500 |
| 10M | 2 VM（8 vCPU）+ RDS m6g.xlarge + Replica + ElastiCache r6g.large | ~$2,000 |
| 50M | 4 VM + RDS m6g.2xlarge + 3 Replica + Redis Cluster | ~$8,000 |
| 100M | 8 VM（2 Regions）+ Aurora Global + Redis Cluster（6 nodes）+ CloudFront | ~$25,000 |
| 500M | 20 VM（4 Regions）+ Aurora Global（4 Region）+ CloudFront + DynamoDB（archive）| ~$100,000 |
| 1B | 40+ VM（8 Regions）+ Aurora + CloudFront + S3 cold storage | ~$250,000+ |

> 以上為粗估，不含 CDN 流量費、頻寬、人力、監控等。
> CloudFront redirect 流量費：~$0.0085/10K requests；1B DAU 月流量費 ≈ $40,000/月。

---

## 七、本架構的天然優勢與限制

### 天然優勢

1. **Redirect 是純讀取 + 靜態映射**  
   URL 一旦建立很少改變 → 極高 CDN hit rate（> 95%）→ App 層成本隨規模邊際遞減

2. **Create 負載極低**  
   即使 1B DAU，create 峰值只有 347 req/s，遠低於任何規模的 app 容量

3. **Cache-friendly 存取模式**  
   QR code 使用符合 Zipf 分佈（少數熱門 QR code 佔大量掃描），CDN 命中率隨規模增加

4. **App 層無狀態**  
   任意增加 app 容器/主機，不需要 session sync

### 限制與挑戰

1. **Cache Invalidation 複雜性**  
   `expires_at`、URL 更新、token 撤銷 → 需要全球 PURGE 同步，延遲窗口期間可能返回過期 302

2. **DB Storage 線性增長**  
   10B+ records 在 3–5 年後需要 cold storage tiering（S3 + DynamoDB on-demand）

3. **Analytics 精確性 vs. 效能取捨**  
   scan_events 若每次掃碼都記錄，在 1B DAU 下就是 5B writes/day。
   實際需要改為 sampling（取樣 1%）+ 聚合計數器（Redis INCR），不能每次都寫 DB。

---

## 八、快速參考：各規模最小配置

```
DAU × 0.000174 = 峰值 redirect QPS

所需 site 數（含 CDN）= ceil(峰值 QPS / 5100)  ← Phase 11c 實測
所需 site 數（不含 CDN）= ceil(峰值 QPS / 2600)  ← Phase 9 實測

Redis 記憶體（GB）= 活躍 token 數 × 256B / 1e9
  ≈ DAU × 0.001 × 256 / 1e9 GB（假設 0.1% DAU 有活躍 token）

DB create QPS = DAU × 3.47e-7（峰值）
DB storage（GB/年）= DAU × 0.01 × 500 × 365 / 1e9
```

| DAU | 峰值 redirect | CDN sites | Redis | DB storage/年 |
|-----|-------------|-----------|-------|--------------|
| 100K | 17 req/s | 1 | 256MB | 0.18GB |
| 1M | 174 req/s | 1 | 256MB | 1.8GB |
| 10M | 1,736 req/s | 1 | 1GB | 18GB |
| 50M | 8,681 req/s | 2 | 5GB | 91GB |
| 100M | 17,361 req/s | 4 | 10GB | 182GB |
| 500M | 86,806 req/s | 17 | 50GB | 912GB |
| 1B | 173,611 req/s | 34 | 100GB | 1.83TB |
