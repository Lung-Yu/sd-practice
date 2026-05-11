# Phase 7 結果：Negative Caching、expires_at Bug 修正、PgBouncer + Nginx 調優

**實施日期：** 2026-05-10

## 改動摘要

Phase 6 確認瓶頸已移至 PostgreSQL 單節點 write throughput，同時也發現兩個正確性問題與數個可進一步調優的配置點。Phase 7 分成兩個方向改動：**正確性修正**與**效能改善**。

### 正確性修正

#### 1. expires_at Bug（`scaffold/app/routes.py` + `scaffold/app/cache.py`）

**問題：** `redirect()` 的 cache-hit 路徑在 Redis 命中時，直接將快取的 URL 回傳為 302 redirect，**完全不檢查 expires_at**。一旦某條短碼已過期，其 URL 仍會在 Redis 中殘留至 TTL 自然過期（預設 86,400s = 24 小時），在此期間對任何請求皆回傳 302，應回傳 410 Gone 的過期 URL 變成永久有效的重導向。

**修正方式：**
- `set_cached_url` 新增 `ttl` 參數（預設 86400s）
- `redirect()` 寫入快取時，若記錄有 `expires_at`，計算 `expires_at - now` 作為 TTL 傳入 `set_cached_url`，確保 Redis 的 key 在 URL 過期的同一時刻自動失效
- `create_qr()` 同樣在建立短碼時傳入正確 TTL，確保新建短碼的快取也受 expires_at 約束

#### 2. Negative Caching（`scaffold/app/cache.py` + `scaffold/app/routes.py`）

**問題：** 每次請求不存在或已刪除的短碼（token），系統都會執行一次完整的 DB 查詢（SELECT），確認後才回傳 404/410。在壓測中，probe 路徑（10% 流量）全部命中不存在的短碼，等同於每個 probe request 都消耗一次 DB 連線，p50 約 1,500ms，且這些 DB 查詢完全無助於業務邏輯。

**修正方式（`gone:{token}` key 設計）：**
- `cache.py` 新增 `is_cached_gone(token)` 與 `set_cached_gone(token, ttl=60)` 兩個函式，使用 `gone:{token}` 作為 Redis key，TTL 固定 60 秒
- `routes.py` 的 `redirect()` 在執行 DB 查詢**之前**，先呼叫 `is_cached_gone(token)`；若命中則直接回傳 404，完全跳過 DB
- DB 查詢得到 404（不存在）或 410（已刪除）時，寫入 `set_cached_gone(token)`
- `delete_cached_url` 同時清除 `gone:{token}`，確保刪除操作後 negative cache 立即失效，不影響後續重新建立同一 token 的正確性

---

## Negative Cache Smoke Test 結果

對同一個不存在的 token（`INVALID_TOKEN`）連續發送三次請求：

| 請求次序 | 回應時間 | 路徑 |
|---------|---------|------|
| 第 1 次 | 79.5ms | DB lookup（無快取，查詢 DB 後寫入 negative cache） |
| 第 2 次 | 3.7ms | Redis negative cache 命中（完全跳過 DB） |
| 第 3 次 | 3.4ms | Redis negative cache 命中（完全跳過 DB） |

**加速比：21x**（79.5ms → 3.5ms）

---

## 效能改善

### 3. PgBouncer `MAX_CLIENT_CONN` 1000 → 2000（`docker-compose.yml`）

Phase 6 分析指出 2 containers × 4 workers × pool_size 100 = 800 潛在 client 連線，PgBouncer 的 `MAX_CLIENT_CONN=1000` 已使用約 80%，高並發下連線等待佇列開始影響 create 路徑。將上限提升至 2000，提供更充裕的連線餘量。

### 4. Nginx keepalive 調優（`nginx/nginx.conf`）

在 upstream block 加入：
- `keepalive_requests 1000`：每條長連線最多服務 1,000 個 request，減少 upstream TCP 重建頻率
- `keepalive_timeout 65s`：長連線閒置 65 秒後才關閉，與 HTTP keepalive 標準值對齊

### 5. uvicorn graceful shutdown（`scaffold/Dockerfile`）

啟動命令加入 `--timeout-graceful-shutdown 30`，確保 rolling restart 或容器停止時，uvicorn 給予進行中的 request 最多 30 秒完成，避免 restart 期間的 502。

---

## 效能對比：Phase 6 vs Phase 7

| 指標 | Phase 6 | Phase 7 | 改善幅度 |
|------|---------|---------|---------|
| avg throughput | 1,471 req/s | 1,716 req/s | +16.7% |
| Dropped iterations | 305,998 (45.8%) | 245,599 (36.74%) | -9.1pp |
| redirect p50 | 23ms | 23ms | 持平 |
| create 成功率 | 99.99% | 99.999% | 持平 |
| probe(not_found) p50 | ~1,500ms | 67ms | -95.5% |
| App errors | 0 | 0 | 持平 |

**完整壓測數據（Phase 7）：**

| 指標 | 數值 |
|------|------|
| Total requests | 422,100 |
| Avg throughput | 1,716 req/s |
| Dropped iterations | 245,599 (36.74%) |
| redirect 成功率 | 100% |
| create 成功率 | 99.999%（1 transient 502，startup 期間） |
| probe 成功率 | 100%（k6 check passes） |
| http_req_failed | 10.02%（全為 probe 404，應用層零錯誤） |
| redirect p50 / p95 / p99 | 23ms / 231ms / 691ms |
| create p50 / p95 | 4,680ms / 8,924ms |
| probe(not_found) p50 / p95 | 67ms / 398ms |

> `http_req_failed 10.02%` 全為 k6 腳本中刻意對不存在短碼發送的 probe 請求（404），屬預期行為，非系統錯誤。

---

## 分析

### probe 路徑大幅改善（p50: 1,500ms → 67ms，-95.5%）

Negative cache 的效果最為直接：第 1 次 probe 仍需 DB 查詢（約 79ms），但從第 2 次起完全命中 Redis（約 3–4ms）。在持續壓測中，大多數 probe 請求的 token 都已被快取，平均 p50 降至 67ms，p95 從估計的數秒降至 398ms。

### throughput +16.7% 的原因

Probe 請求佔總流量約 10%，Phase 6 中這 10% 的流量全數消耗 DB 連線（每個 probe = 一次 SELECT）。Negative cache 上線後，這些 DB 查詢幾乎消失，相當於釋放了約 10% 的 PgBouncer 連線給 create 路徑使用，減輕了連線等待佇列的壓力，整體 throughput 因此提升 16.7%。

### PgBouncer MAX_CLIENT_CONN 2000

連線上限從 1,000 提升至 2,000，消除了高並發峰值時的連線等待，與 probe 路徑節省的連線數共同作用，改善了 create 路徑的穩定性（成功率從 99.99% 提升至 99.999%）。

### Nginx keepalive 調優

`keepalive_requests 1000` 與 `keepalive_timeout 65s` 減少了 Nginx 到 upstream 的 TCP 重建次數，對 redirect 路徑的 p99（691ms，Phase 6 為 1,742ms）有明顯改善。

### expires_at bug 修正

此修正是**正確性改善**，確保過期 URL 不再被無限期快取並繼續重導向。不直接影響 QPS，但消除了一個潛在的業務邏輯漏洞——若不修正，任何設有 expires_at 的短碼在過期後 24 小時內仍可被訪問，與設計語意不符。

---

## 結論

Phase 7 透過兩個方向的改動，以極低的實作成本帶來可觀的效果：

1. **正確性維度**：expires_at TTL 修正確保過期短碼在 Redis 中的生命週期與業務語意一致；negative cache 的設計（gone:{token}, TTL=60s）防止無效 token 請求穿透至 DB，也在刪除操作時正確清除快取，不影響後續的重新建立。

2. **效能維度**：Probe 路徑 p50 從 ~1,500ms 降至 67ms（-95.5%），整體 throughput 從 1,471 req/s 提升至 1,716 req/s（+16.7%），Dropped iterations 從 45.8% 降至 36.74%。

系統現在在 **correctness** 和 **performance** 兩個維度都更為健壯。Negative caching 是這次最高 ROI 的改動——**消除不必要的 DB 查詢，永遠比優化 DB 查詢本身更有效率。**
