# Phase 11c — CDN 本地模擬：Varnish 快取 302 redirect（2026-05-12）

## 目標

Phase 11a 確認單機 app ceiling 為 ~2,600 req/s；Phase 11b 驗證多 site 路由機制正確。
本 Phase 用 Varnish 模擬 CDN：將 302 redirect 快取在記憶體，讓大部分流量在 CDN 層直接回應，
不打穿到 app/Redis 路徑。目標：突破 5,000 QPS 原始目標。

---

## 架構

```
k6（本機）
    │
Varnish（port 8200，cache 256 MB）
    │                    │
 MISS（第一次）      HIT（後續 99%+）
    │                    │
nginx-origin             └── 直接從記憶體回傳 302
    │
app1~app4（各 4 workers）→ Redis → 302
```

### 快取策略（VCL）

| 條件 | 行為 |
|------|------|
| `GET /r/<token>` → 302 | 快取，TTL=60s |
| `GET /r/<token>` → 404 | 不快取（uncacheable）|
| `POST /api/qr/create` | 直接 pass（POST 不快取）|
| 其他路徑 | pass |

觀察 header：`X-Cache: HIT/MISS`，`X-Cache-Hits: N`

---

## 測試結果

| 指標 | Phase 11a（無 CDN）| Phase 11c（Varnish CDN）| 改善 |
|------|-------------------|-----------------------|------|
| **avg throughput** | 2,255 req/s | **3,165 req/s** | +40% |
| **peak throughput**（hold@6000 stage）| ~2,550 req/s | **~5,100-5,200 req/s** | **+100%** |
| **p50 latency** | 29ms（app+Redis）| **0.202ms**（Varnish memory）| **144x 改善** |
| **p90 latency** | — | **1.53ms** | — |
| **p95 latency** | 4.01s（飽和）| **39.7ms**（閾值 < 500ms ✓）| — |
| **http_req_failed** | 0.04% | **0.02%** | — |
| **checks（→302）** | 100% | **100%** | — |
| **dropped_iterations** | 184,679 | **24,578**（-87%）| — |
| **Thresholds** | ✗ | **✓ 全通過** | — |
| max VUs needed | 5,000 | **3,336**（提前滿足目標）| — |
| seeded tokens | 340 | 344 | — |

### Peak 期間量化

在 k6 "hold at 6000 req/s" 的 60 秒窗口（t=1m36s ~ t=2m36s）：
- 迭代數：192,500 → 505,000 = 312,500 iterations in 60s = **~5,208 req/s**
- 此期間 VU 佔用數：1~5（幾乎無 backpressure）
- 系統幾乎完美消化了 6,000 req/s 的請求目標（部分到達率限制是 k6 自身的 scheduler）

---

## 技術分析

### 為什麼 CDN 效果如此顯著

**原來的路徑（無 CDN）：**
```
每個 redirect request：
  1. 穿越 Podman bridge network → nginx（~0.1ms）
  2. nginx → app（~0.1ms）
  3. Python FastAPI 解析 + 路由（~0.1ms CPU）
  4. Redis GET（~0.5ms network round trip）
  5. 組 302 response + 返回（~0.1ms）
總計 RTT ≈ 1~2ms；CPU 消耗：Python + Redis
```

**Varnish HIT 路徑：**
```
每個 redirect request：
  1. 穿越 Podman bridge network → Varnish（~0.1ms）
  2. Hash lookup（VCL）+ 返回快取的 302（< 0.01ms）
  3. 返回（~0.1ms）
總計 RTT ≈ 0.2ms；CPU 消耗：Varnish（C，event-driven，極低）
```

### p50 = 0.202ms 的意義

0.2ms p50 ≈ 純網路延遲（Podman TAP interface 開銷）+ Varnish hash lookup（< 10μs）。
這就是 CDN 在理想情況下的真實表現：**快取命中 = 記憶體讀取 + 網路 RTT**。

app 層（Python + Redis）完全不在 critical path，app containers 在 HIT 期間幾乎閒置。

### Cache Miss 率與 warm-up

- 500 個 unique tokens，test 開始後第一輪 500 requests = MISSes
- 在 ~1,000 req/s 起點，500 MISSes 需 ~0.5s 完成 → 快取在 test 開始後 < 1 秒即全暖
- 後續 555,000+ requests 中的 MISSes 主要是 TTL=60s 過期重新填充
- 整體 cache hit rate 估計 > 99.9%

p95 = 39.7ms（vs p90 = 1.53ms）：p95 以上的 requests 就是那少數 MISSes（走完整 nginx-origin → app 路徑），說明 MISS 路徑需要 ~40ms，而 HIT 路徑只需 0.2ms。

---

## CDN 的取捨

| 面向 | 優點 | 限制/風險 |
|------|------|---------|
| 吞吐量 | ~5,100 req/s peak（+100%）| 受 CDN 記憶體大小限制（256MB 可快取 ~2M 個 302 responses）|
| 延遲 | p50 0.202ms（144x 改善）| Cache MISS 路徑延遲更高（需打穿到 origin）|
| App 負載 | App containers 幾乎閒置（HIT 期間）| Origin 需要能承受 cache miss 風潮（cold start / TTL expire）|
| 正確性 | 302 redirect 是靜態數據，快取安全 | 若 QR code expires_at 或 destination_url 更新，需主動 purge |
| 成本 | 大幅降低 App 伺服器數量需求 | 需要 CDN 基礎設施（自建 Varnish 或雲端 CDN）|

### 什麼時候需要 Cache Purge

本系統的 `UrlMapping` model 有 `expires_at` 欄位。若一個 token 在 Varnish 快取中（TTL=60s），
但 `expires_at` 在這 60s 內到期，Varnish 仍會返回 302（舊快取），用戶收到已過期的重定向。

**生產環境解法：**
1. 在 `expires_at` update 時，向 Varnish 發 `PURGE` 請求（`PURGE /r/<token>`）
2. 或縮短 TTL（代價：更多 MISSes，app 負載增加）
3. 在 app 的 redirect handler 加 `Cache-Control: max-age=N`，讓 Varnish/CDN 自動尊重

---

## 與原始目標的對照

**原始目標：redirect throughput ≥ 5,000 QPS**

| 方案 | Peak QPS | 達成目標 |
|------|---------|---------|
| 單機 app（Phase 9/11a） | ~2,600 req/s | ✗（未達）|
| 真實多主機 2 sites（Phase 11b 理論估算）| ~5,200 req/s | ✓（理論）|
| 單機 + Varnish CDN（Phase 11c）| **~5,100 req/s** | **✓（實測）** |

CDN 是**在不增加 app 主機的情況下達成 5,000 QPS 目標的最高效路徑**。
真實部署中，兩者組合（多主機 + CDN）可以進一步擴展。

---

## 新增檔案

| 檔案 | 內容 |
|------|------|
| `varnish/default.vcl` | VCL：快取 GET /r/* 的 302，不快取 POST 和 4xx |
| `docker-compose.yml` | 新增 nginx-origin（app1-4 single-tier）+ varnish（port 8200）|

---

## 學到的系統設計概念

### 1. CDN 的本質：把「計算」換成「記憶體查找」

每個 app-path redirect 需要：Python 解析 + Redis 查詢 + asyncio 調度 = ~1ms CPU + network。
每個 CDN HIT 需要：hash table lookup = ~10μs CPU（純記憶體）。

當 90%+ 的請求都是相同 token 的重複查詢（真實 QR code 使用場景），CDN 的效益是最大的。
**CDN 解決的不是容量問題，而是工作重複性問題。**

### 2. Cache Hit Rate 是 CDN 效益的核心指標

CDN 效益 ∝ cache hit rate：
```
有效吞吐量 = (hit_rate × CDN_throughput) + (miss_rate × origin_throughput)
本次: ≈ (0.999 × 5000) + (0.001 × 2600) = 4997 + 2.6 ≈ 5000 req/s
```

影響 hit rate 的因素：
- **Token diversity**：有多少 unique token 在 active traffic 中
- **TTL**：越長 hit rate 越高，但正確性風險越高
- **Cache 大小**：需足夠容納 active token set

本次：500 tokens，256MB 快取，TTL=60s → 幾乎 100% hit rate。
生產環境（數億 tokens）：需要 LRU 快取，只有 "hot" tokens 在 CDN 中。

### 3. 為什麼 CDN 比加 App 容器更高效

| 方式 | 成本 | 效益 |
|------|------|------|
| 加一台 App VM（+1 site）| 1 台 VM（CPU + memory + 費用）| +2,600 req/s |
| 加 CDN（Varnish 或雲端 CDN）| 少量記憶體（256MB for 2M tokens）| +2,600 req/s（等效）|

CDN 的邊際成本極低：快取 1M 個 302 responses ≈ 1M × 256 bytes = 256MB RAM。
而 app VM 需要完整的 CPU、OS、Python runtime、Redis 連線等。

**對於 read-heavy、資料相對穩定的工作負載（如 QR code redirect），CDN 是性價比最高的擴展手段。**

---

## 最終 Phase 11c 狀態

| 項目 | 值 |
|------|-----|
| CDN 層 | Varnish 6.0，256MB memory，TTL=60s |
| redirect peak QPS（Varnish HIT）| **~5,100 req/s（達成 5,000 QPS 目標）** |
| redirect p50（HIT）| **0.202ms** |
| redirect p50（MISS / origin path）| ~40ms |
| App 容器負載（HIT 期間）| 接近閒置 |
| 下一步 | Phase 11d：100M+ DAU 規模估算文件 |

## Commits

- `80125b9` — `feat(phase11c): Varnish CDN achieves 5000 QPS — 5100 req/s peak, p50=0.202ms`
