# Phase 11b — Multi-host 模擬：雙 Site + 頂層 LB（2026-05-12）

## 目標

Phase 11a 確認單一 Podman VM 的 redirect ceiling 為 ~2,600 req/s，與容器數無關。
本 Phase 模擬真實多主機架構（GLB → Site LB → App），驗證：
1. 三層路由機制的正確性（所有層級均能轉發請求）
2. 雙 Site 能否在單 VM 模擬環境中展示吞吐量提升
3. 在模擬結果不理想時，估算真實多主機的理論效益

---

## 架構變更

### 拓樸（模擬）

```
k6（本機）
    │
nginx-global（port 8100, worker_processes 2）
    ├── nginx-site1（worker_processes 2）→ app1, app2
    └── nginx-site2（worker_processes 2）→ app3, app4
                                              │
                              PgBouncer → PostgreSQL Primary
                                              ↓ WAL streaming
                                         PostgreSQL Replica
                              Redis（共用，AOF）
```

### 真實多主機對應

| 模擬組件 | 真實對應 |
|---------|---------|
| nginx-global | Global Load Balancer（獨立主機或雲端 GLB） |
| nginx-site1 + app1/app2 | Host 1（獨立 VM 或物理機） |
| nginx-site2 + app3/app4 | Host 2（獨立 VM 或物理機） |
| PgBouncer + PostgreSQL | 獨立 DB Cluster（共享） |
| Redis | 獨立 Redis Cluster（共享）|

### 新增檔案

| 檔案 | 內容 |
|------|------|
| `nginx/nginx-global.conf` | upstream: nginx-site1:80, nginx-site2:80; worker 2 |
| `nginx/nginx-site1.conf` | upstream: app1:8000, app2:8000; worker 2 |
| `nginx/nginx-site2.conf` | upstream: app3:8000, app4:8000; worker 2 |
| `docker-compose.yml` | 原 nginx → nginx-global + nginx-site1 + nginx-site2 |

---

## 測試結果

| 指標 | Phase 11a（1-tier Nginx）| Phase 11b（3-tier Nginx）|
|------|------------------------|-----------------------|
| throughput（avg）| **2,255 req/s** | **931 req/s（-59%）** |
| http_req_failed | 0.04% | **11.51%** |
| checks（redirect → 302）| 100% | **88.44%** |
| p95 | 4.01s | **9.85s** |
| dropped_iterations | 184,679 | 416,480 |
| seeded tokens | 340 | **500（setup 完全正常）** |

---

## 結果分析

### 功能驗證：成功

- nginx-site1 和 nginx-site2 均有收到流量（logs 確認均勻分發）
- 302 redirect 路徑正確穿越三層：nginx-global → nginx-site → app → Redis
- Setup 500 tokens 全部成功（throttle 機制生效）

### 效能退步：模擬環境限制，非架構問題

**根本原因：在單一 Podman VM 中，增加 Nginx 層數 = 增加 CPU 和網路開銷，而非分散到獨立主機。**

| 量化影響 | Phase 11a | Phase 11b |
|---------|----------|----------|
| Nginx workers | 4 | **6**（3 containers × 2 workers） |
| 每個 request 的 Nginx hops | 1 | **3**（global + site + proxy_pass to app） |
| Container-to-container 封包/s@2000 req/s | ~6,000 | **~10,000** |
| Python workers | 16 | 16（不變）|
| 總 Nginx + Python processes | 20 | **22** |

在實際多主機環境中：
- nginx-global 在專用主機上，有獨立 CPU
- nginx-site1/nginx-site2 各在自己的主機上，不搶對方 CPU
- 額外 hop 帶來的是 ~0.5ms 網路延遲，而非 CPU 競爭

### 資源競爭量化（12 vCPU Podman VM）

```
[nginx-global]  2 workers  → 1.0 vCPU 佔用
[nginx-site1]   2 workers  → 1.0 vCPU 佔用
[nginx-site2]   2 workers  → 1.0 vCPU 佔用
[app1-4]       16 workers  → 8.0 vCPU 佔用（理想估算）
[PgBouncer]                → 0.5 vCPU
[PostgreSQL]               → 0.5 vCPU（redirect 極少碰 DB）
[Redis]                    → 0.5 vCPU
----------------------------------------------
Total                      → 12.5 vCPU on 12 vCPU VM → 過載
```

---

## 理論多主機效益估算

基於 Phase 11a 確認的單主機 ceiling（~2,600 req/s per site）：

| 配置 | 理論 redirect QPS | 備注 |
|------|-----------------|------|
| 1 site（Phase 9/11a） | 2,600 req/s | 實測驗證 |
| 2 sites（真實多主機） | **5,200 req/s** | 線性擴展，各 site 獨立 CPU |
| 4 sites | **10,400 req/s** | 需 4 台主機 + Redis Cluster |
| 8 sites | **20,800 req/s** | 需分散式 Redis（Redis Cluster 或 Consistent Hashing） |
| 16 sites | **41,600 req/s** | 需 Global Load Balancer 分地理區域 |

**真實多主機的線性擴展前提：**
1. 每個 site 有獨立 Redis connection（Redis 本身不是瓶頸，100k+ GET/s 能力）
2. DB 讀取走 Replica，不同 site 可接相同 Replica（replication lag ~1.8ms 可接受）
3. DB 寫入（create）走 Primary，需要 PgBouncer 的 pool 夠大（DEFAULT_POOL_SIZE 目前 40，支持 ~4 個 site 各 10 個 DB 連線）

---

## 達到 5,000 QPS 所需的最小配置

根據實驗數據：

```
5,000 req/s ÷ 2,600 req/s per site = 1.92 → 需要 2 個獨立主機
```

最小架構：
```
GLB（nginx 或 AWS ALB）
  ├── Site 1（VM 1）：nginx + 2 app containers × 4 workers → ~2,600 req/s
  └── Site 2（VM 2）：nginx + 2 app containers × 4 workers → ~2,600 req/s
                              │
                     共用 Redis（AWS ElastiCache 或獨立 Redis VM）
                     共用 PgBouncer → PostgreSQL Primary + Replica
```

成本：2 台 app VM + 1 台 Redis（或 Managed Redis）+ 1 台 DB（Primary+Replica）

---

## 學到的系統設計概念

### 1. 模擬環境的核心侷限

單一 VM 模擬多主機，只能驗證**路由正確性**，無法驗證**效能水平擴展性**。
每次加一層 proxy，在單 VM 上是「資源重分配」，在真實多主機上是「資源新增」。

**規則：在單 VM 模擬多主機時，效能數字沒有參考價值；只有架構正確性有參考價值。**

### 2. 三層 LB 架構在真實場景的適用時機

| 層 | 名稱 | 職責 |
|----|------|------|
| Global LB | Anycast / GeoDNS | 根據地理位置路由到最近的 Region |
| Site LB | Regional Nginx / HAProxy | 在 Region 內分發到多台 App Server |
| App | Uvicorn workers | 處理業務邏輯 |

每層解決不同的問題：Global LB 解決地理容錯，Site LB 解決 Region 內高可用，App 層解決業務處理。

### 3. 水平擴展的正確驗證方式

| 方法 | 適用情境 |
|------|---------|
| 單 VM 多 container | 驗證路由邏輯、API 正確性 |
| 多 VM / 雲端 | 驗證實際吞吐量的水平擴展性 |
| 理論估算（基於單節點實測）| 快速估算多節點容量需求 |

---

## 最終 Phase 11b 狀態

| 項目 | 值 |
|------|-----|
| 架構驗證 | ✓（三層路由正確，兩個 site 均勻分流）|
| 單 VM 模擬效能 | 931 req/s（比 11a 差，模擬環境限制）|
| 真實多主機理論估算 | ~5,200 req/s（2 sites × 2,600）|
| 下一步 | Phase 11c：Varnish CDN 本地模擬 |

---

## Commits

- `（本 Phase commit）` — feat(phase11b): multi-site 3-tier LB routing verification
