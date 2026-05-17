# Key Learnings — Notification System (Tier 1–8)

> 本文整理整個優化過程（T1–T8）中，最值得帶走的知識點。
> 不是 changelog，是可以在下個系統設計題直接套用的思維框架。

---

## 1. 同步 vs 非同步：解耦要跨 process，不是跨函式

**陷阱**：把 `deliver()` 丟進 `BackgroundTasks` 看起來像「非同步化」。
FastAPI 的 `BackgroundTasks` 對 sync 函式用的是同一個 thread pool，背景任務只是把債務往後移，在高壓下 p95 反而變差。

```
錯誤：HTTP worker → BackgroundTask → deliver()    # 同 pool，沒真正隔離
正確：HTTP worker → Redis XADD → return 202        # delivery worker 是獨立 container
       delivery-worker → XREADGROUP → deliver()
```

**帶走的原則**：解耦必須是跨 process。同一個 process 內排後面不算解耦。

---

## 2. Redis 是單執行緒：命令數 = 真實瓶頸

所有「scaling 沒帶來好處」的根本原因都是同一個：

> **Redis 是單執行緒。N 個 worker 同時送命令不會讓 Redis 變快，只會讓每個命令等更久。**

Tier 6 失敗（POST p95 1,450ms）的計算：

```
4 workers × 20 concurrent deliveries = 80 concurrent pipelines

per message (×80):
  HGETALL notification:{id}   → 1 round-trip to primary Redis
  HSET + SET + ZADD pipeline  → 3 commands to primary Redis
  XACK                        → 1 command to delivery Redis
```

Tier 8 全部優化後：

```
per batch (4 workers × 1 batch each):
  abatch_get([20 nids])  → 4 pipeline round-trips (各帶 20 HGETALL)
  save_status()          → 80 HSET（3 commands 降到 1）
  batch XACK             → 4 XACK（20 個 msg_id 一起送）
```

同樣的硬體，Redis 命令壓力差了一個數量級，結果從 1,450ms → 433ms。

---

## 3. num_workers × BATCH_SIZE = 常數（Single-Redis 的黃金法則）

```
total_concurrent_deliveries = num_workers × BATCH_SIZE
```

這個乘積決定 Redis 的命令佇列深度。

| 配置 | 乘積 | POST p95 |
|------|------|----------|
| 1w × BS=20 | 20 | 361ms ✓ |
| 4w × BS=20 | 80 | 1,450ms ❌ |
| 4w × BS=5  | 20 | 351ms ✓ |
| 4w × BS=20 + all opts | 等效 ↓ | 433ms ✓ |

**BATCH_SIZE 是 config fix，不是 scaling fix**：4w × BS=5 的交付吞吐和 1w × BS=20 一樣，真正的好處是容錯（1 個 worker 掛 = 25% 損失，不是 100%）。

---

## 4. Little's Law 解釋 throughput ceiling

```
VUs = RPS × avg_latency_s
→ RPS_max = VU_cap / avg_latency_s
```

- k6 設定 600 VUs cap、目標 5000 RPS
- avg latency = 400ms → 實際最大 = 600 / 0.4 = **1500 RPS**
- latency 升高 → 分母變大 → RPS 下降（saturation 下的自我加劇）

這也解釋為什麼 latency 優化和 throughput 優化不是兩件事，而是同一件事。

---

## 5. async routes 是最高 ROI 的優化

同一個 container（不加機器），async `def` + `redis.asyncio` 的效果：

| 指標 | sync | async | 變化 |
|------|------|-------|------|
| POST p95 | 466ms | 283ms | −39% |
| GET p95 | 450ms | 137ms | −69% |
| Throughput | 2,070 RPS | 3,072 RPS | +48% |

**原因**：sync route 佔一個 thread 整個請求期間；async route 只在 `await` 時讓出 event loop，實際用 CPU 的時間是 microseconds。

---

## 6. nginx 對 READ vs WRITE 的影響是不對稱的

加 nginx + 4 container replicas 的效果：

| Endpoint | 加 nginx 後 | 原因 |
|----------|------------|------|
| GET /{id} | −26% latency ✓ | 讀取真正並行化 |
| POST /send | +27% latency ❌ | nginx hop + write 壓力集中在同一個 Redis |

**結論**：IO-bound 寫入路徑的瓶頸是下游 Redis，加更多 worker 只加了競爭。  
垂直 scale（1 大 container + 更多 uvicorn workers）勝過水平 scale（N 小 container + nginx）在 IO-bound 場景。

---

## 7. Redis Pipeline 的正確使用

**Pipeline = 把多個命令一次送出，等一次回應。**

```python
# ×N round-trips
for nid in nids:
    result = await r.hgetall(f"notification:{nid}")

# ×1 round-trip
pipe = r.pipeline()
for nid in nids:
    pipe.hgetall(f"notification:{nid}")
results = await pipe.execute()
```

- 省下的是 **RTT（round-trip time）**，不是 Redis 的 CPU time。Redis 仍然逐一執行命令。
- Pipeline vs 並發 coroutine：並發允許「讀+交付」重疊，Pipeline 要等所有讀完才開始交付。BS 小（≤5）且 RTT ~1ms 時 Pipeline 勝；BS 大或 RTT 高時差異更顯著。

---

## 8. 只寫改變的欄位（Write What Changes）

通知建立時：寫 HSET（全欄位）+ SET（idempotency key）+ ZADD（user set）。

交付後：idempotency key 和 user ZADD **永遠不變**，只有 `status / sent_at / error / attempts` 改變。

```python
# save()：3 commands（原本）
HSET + SET + ZADD

# save_status()：1 command（優化後）
HSET notification:{id} status sent_at error attempts
```

對 Redis 單執行緒來說，每少一個命令都是真實的延遲收益。

---

## 9. Redis Streams Consumer Group 的運作模型

```
XADD   → 生產者寫入
XREADGROUP GROUP {group} {consumer} COUNT N BLOCK T STREAMS {key} >
          # > = 只拿未被 claim 的新訊息
XACK   → 確認處理完畢，從 pending list 移除
```

**關鍵細節**：

- **Consumer name = `socket.gethostname()`**：docker-compose 每個 container 的 hostname = container ID（唯一），不需要額外協調機制。
- **Exactly-once**：同一個訊息只會被一個 consumer claim，XACK 後不會重新交付。
- **Pending messages**：讀取後未 XACK 的訊息。container 重啟後新 consumer name 不同，舊 pending 訊息被「孤立」，需要 `XAUTOCLAIM` 才能被重新處理。生產環境需要 reaper 定期執行 XAUTOCLAIM。
- **Batch XACK**：`XACK stream group id1 id2 id3 ...`，一次 ACK 整個 batch，命令數從 N 降到 1。
- **Lag = 0 + Pending > 0**：表示所有訊息都已被 deliver 給某個 consumer，但有些未 ACK（可能是 consumer 中途掛了）。

---

## 10. 2 Redis 分離：邊界要想清楚

「把 Stream 移到 delivery Redis」只解了一半：

```
移走的：XADD, XREADGROUP, XACK（Stream 命令）
還留著：store.save()（每次交付後寫 primary Redis）
```

Tier 7 只解了 GET（stream 不和讀取競爭），POST 仍超標（delivery write 還在 primary Redis）。

真正解法的兩個方向：
1. **save_status()**：delivery 只寫 1 HSET（不寫 idempotency SET 和 user ZADD），大幅降低 primary Redis 壓力
2. **第三個 Redis**：delivery status 寫入獨立 store，API 讀取時 merge（更複雜，非當前 scale 所需）

**帶走的原則**：分離必須分離到 write path，不只是 read/write 隔離。

---

## 11. Redis 啟動時 PING ≠ 資料命令 Ready

Redis 7 在 AOF replay 期間：
- `PING` → `PONG`（立刻回應）
- `HGETALL` / `HSET` / `GET` → `LOADING`（錯誤）

```python
# 這樣不夠，PING 成功不代表資料命令可用：
await r.ping()

# 正確：捕捉 BusyLoadingError，才是真正的 ready 訊號
try:
    await r.ping()
    return  # 成功才算
except (redis.exceptions.BusyLoadingError, redis.exceptions.ConnectionError):
    await asyncio.sleep(delay)
```

**影響**：
- Worker 只等 delivery Redis → `abatch_get()` 打 primary Redis 失敗 → 緊湊錯誤迴圈 → 大量 pending 訊息堆積
- API 沒等 Redis 就接請求 → k6 `setup()` POST 全失敗 → `seedIds = []` → 所有 GET 用 fallback UUID → 37K 404

解法：等兩個 Redis，都要真正 ready。

---

## 12. Redis Keyspace 大小是隱藏的延遲變數

Primary Redis 從 0 累積到 4M keys → POST p95 從 408ms 升到 759ms（近 2 倍）。

**原因**：
- 更大的 hash table → key lookup 微增，大量累積後可測量
- 更多記憶體 → container memory pressure
- 更長的 AOF replay → startup LOADING window 變長

**生產解法**：
```python
# 建立通知時設 TTL（7 天）
pipe.expire(f"notification:{nid}", 7 * 24 * 3600)
```
```
# Redis 設定
maxmemory-policy: volatile-lru
```

**Benchmark 的啟示**：用「乾淨的 Redis」跑基準測試，才能比較不同優化的真實效果。在「髒的」狀態下的 regression 很可能是 keyspace 污染，不是 code 的問題。

---

## 13. Connection Pool 的大小計算

```
required_pool ≥ peak_concurrent_requests × max_simultaneous_redis_ops_per_request
```

- 600 VUs → 同時最多 600 個 request，各需最多 2 個 Redis ops → 需要 ≥ 1200 connections
- 預設 `max_connections=100` → 在 600 VU 下立刻爆出 `ConnectionError: Too many connections`

**容易誤診的地方**：`redis.exceptions.ConnectionError: Too many connections` 是 **client-side** pool 耗盡，不是 Redis server 的問題。兩者 log 看起來一樣，但前者是調 `max_connections` 解決，後者需要擴 Redis。

**redis-py 的行為**：pool 耗盡時直接丟 exception，不排隊等待。所以 max_connections 要直接設夠大，不能依賴排隊緩衝。

---

## 14. 電路斷路器與 DLQ 的交互作用

```
CLOSED → (N 次連續失敗) → OPEN (fast-fail)
OPEN   → (recovery_s 後) → HALF_OPEN (probe)
HALF_OPEN → (成功) → CLOSED
HALF_OPEN → (失敗) → OPEN
```

**DLQ 堆積量遠高於理論值的原因**：

- 理論：FAILURE_RATE=0.2，3 次重試全失敗 = 0.2³ = 0.8%
- 實際：CB OPEN 後直接 fast-fail，**跳過所有重試** → DLQ 堆積速度遠高於 0.8%
- Circuit breaker 和 retry 的交互作用：CB 是為了保護 thread capacity，但副作用是 bypass retry，所以 DLQ 會比「純計算」多很多

**DLQ + retry API 的 ops playbook**：
1. DLQ depth 告警觸發
2. 確認 channel 健康（CB 狀態、外部服務）
3. 等 channel 恢復
4. `POST /admin/dlq/retry?count=N` 分批重送
5. 監控 `notifications_sent_total{status="SENT"}` 上升、DLQ depth 降到 0

---

## 15. 可觀測性是優化的前提

沒有 metrics 就看不到瓶頸在哪裡。每次「加了東西但不知道有沒有用」都是因為缺乏量測。

**最有用的 metrics**：

| Metric | 用途 |
|--------|------|
| `notifications_sent_total{channel, status}` | 成功/失敗分布 |
| `notification_delivery_seconds{channel}` | 各 channel 交付延遲 |
| `notification_retries_total{channel}` | retry 頻率（越高 = channel 越不穩定）|
| `delivery_timeouts_total{channel}` | timeout 頻率 |
| `circuit_breaker_trips_total{channel}` | CB 觸發頻率（>0 = 需要關注）|
| `rate_limit_hits_total` | 429 頻率（持續高 = abuse 或 client 設定問題）|

**k6 + Prometheus remote write**：`K6_PROMETHEUS_RW_TREND_AS_NATIVE_HISTOGRAM=false` 輸出 `_p99` gauge，不是 `_bucket` histogram。用 `histogram_quantile()` 查不到資料，要直接用 `metric_name_p99`。

---

## 完整效能進化

| Tier | 關鍵改變 | POST p95 | 通過? |
|------|---------|----------|-------|
| T2A | Redis store（基準）| 544ms | ❌ |
| T2B | BackgroundTasks | 579ms | ❌ |
| T2C | 獨立 delivery worker + Redis Streams | 466ms | ✓ |
| T3B | async routes + redis.asyncio | 283ms | ✓ |
| T4  | async worker + asyncio.gather() | 361ms | ✓ |
| T5  | FAILURE_RATE=0.2 壓測（CB + DLQ 驗證）| 358ms | ✓ |
| T6  | 4w × BS=20（Redis 飽和）| 1,450ms | ❌ |
| T6a | 4w × BS=5（num_workers × BS = const）| 351ms | ✓ |
| T7  | 2 Redis 分離（Stream 移走）| 623ms | ❌ |
| T7+ | abatch_get + batch XACK + save_status | 430ms | ✓ |
| **T8** | **4w × BS=20 + 全部優化** | **433ms** | **✓** |

T8 的意義：T6 在 BS=20 下失敗的原因不是 BS 太大，而是命令數太多。把命令數降下來後，BS=20 和 BS=5 的延遲相同（430ms ≈ 433ms），但 BS=20 有 4× 的突發流量緩衝空間。
