# Tier 7：分離 Delivery Redis（Stream + DLQ 隔離）

## 架構變更概述

### 變更前

所有操作共用同一個 Redis 實例：

```
REDIS_URL=redis://redis:6379/0
  ├─ API：INCR+EXPIRE（rate limit）
  ├─ API：GET idempotency key（idempotency check）
  ├─ API：pipeline(HSET+SET+ZADD)（save PENDING）
  ├─ API：XADD notifications:delivery（enqueue）
  ├─ Worker：HGETALL（store.aget）
  ├─ Worker：pipeline(HSET+SET+ZADD)（store.save — SENT/FAILED）
  ├─ Worker：XREADGROUP / XACK
  └─ Worker：LPUSH notifications:dlq（DLQ）
```

所有讀寫操作都競爭同一個 Redis 的 command queue，形成單點序列化瓶頸。

### 變更後

```
REDIS_URL=redis://redis:6379/0          ← 主要 Redis（API 狀態）
  ├─ HASH：通知狀態（HSET/HGETALL）
  ├─ STRING：idempotency key（GET/SET）
  ├─ ZSET：per-user 通知列表（ZADD/ZRANGE）
  └─ INCR+EXPIRE：rate limit

DELIVERY_REDIS_URL=redis://redis-delivery:6379/0   ← 獨立 Delivery Redis
  ├─ Stream：notifications:delivery（XADD/XREADGROUP/XACK）
  └─ List：notifications:dlq（LPUSH/LRANGE/LPOP）
```

Stream 操作和 DLQ 從主要 Redis 移出，API 的讀寫不再受到 Stream 操作干擾。

---

## 程式碼異動

### `config.py`

新增 `DELIVERY_REDIS_URL` 設定，並附加 fallback 邏輯：

```python
DELIVERY_REDIS_URL = os.getenv("DELIVERY_REDIS_URL", "") or REDIS_URL
```

設計重點：
- 若環境變數未設定（空字串或未存在），自動 fallback 到 `REDIS_URL`
- 舊部署（Tier 1–6a）完全不需要改動任何設定
- 新部署只需在 `docker-compose.yml` 加一行 `DELIVERY_REDIS_URL`

### `queue.py`

所有 Redis client 改用 `config.DELIVERY_REDIS_URL`：

```python
# 變更前
r = redis.asyncio.from_url(config.REDIS_URL, ...)

# 變更後
r = redis.asyncio.from_url(config.DELIVERY_REDIS_URL, ...)
```

影響的操作：`XADD`（enqueue）、DLQ 的 `LPUSH`/`LRANGE`/`LPOP`。

### `worker.py`

XREADGROUP/XACK 的 client 改用 `config.DELIVERY_REDIS_URL`；但 `store.aget()` 和 `store.save()` 仍使用主要 Redis：

```python
# Delivery Stream client → DELIVERY_REDIS_URL
r = await redis.asyncio.from_url(config.DELIVERY_REDIS_URL, ...)

# Store 操作 → REDIS_URL（主要 Redis，不變）
notification = await store.aget(nid)   # HGETALL → primary Redis
await store.save(notification)         # HSET+SET+ZADD → primary Redis ← 仍在主要 Redis！
```

這個「仍在主要 Redis」是 Tier 7 只有部分成效的根本原因（詳見下方分析）。

### `docker-compose.yml`

新增 `redis-delivery` service 和對應的 volume：

```yaml
services:
  redis-delivery:
    image: redis:7-alpine
    command: redis-server --appendonly yes
    volumes:
      - redis_delivery_data:/data
    networks:
      - internal
      - sd_monitoring

volumes:
  redis_delivery_data:
```

delivery-worker 的環境變數新增：

```yaml
delivery-worker:
  environment:
    - DELIVERY_REDIS_URL=redis://redis-delivery:6379/0
```

---

## 測試結果

### 測試配置

- 4 個 delivery-worker 容器（`--scale delivery-worker=4`）
- `BATCH_SIZE=20`（回到 Tier 6 的設定，以驗證 Stream 隔離的效果）
- `FAILURE_RATE=0`、`MAX_RETRIES=1`
- 分離的 delivery Redis

### 數字結果

| 配置 | POST p95 | GET p95 | Throughput | Error Rate | 達標？ |
|------|----------|---------|------------|------------|--------|
| Tier 6：4w BS=20，1 Redis | 1,450ms ❌ | 532ms ❌ | 800 RPS | 0% | ❌ |
| Tier 6a：4w BS=5，1 Redis | 351ms ✓ | 162ms ✓ | 2,838 RPS | 0% | ✓ |
| **Tier 7：4w BS=20，2 Redis** | **623ms ❌** | **281ms ✓** | **1,963 RPS** | **0%** | **❌** |

Target：POST p95 < 500ms，GET p95 < 500ms，Error rate < 1%。

### Worker 工作量分配

| Worker | SENT 數量 |
|--------|-----------|
| Worker 1 | 40,119 |
| Worker 2 | 39,951 |
| Worker 3 | 40,211 |
| Worker 4 | 40,093 |
| **合計** | **160,374** |

分配比例約各 25%，Redis consumer group 的 exactly-once 分發機制正常運作。

---

## 分析：為什麼 Tier 7 只有部分成效？

### 成功的部分

**GET p95 改善顯著**：532ms（Tier 6）→ 281ms（Tier 7），改善 47%。

原因：XREADGROUP/XACK 從主要 Redis 移走，減少了 worker 對主要 Redis 的讀取壓力，GET 請求搶 Redis 資源的競爭者變少。

### 失敗的部分

**POST p95 仍然超標**：623ms > 500ms 目標。

雖然 POST p95 從 1,450ms 改善到 623ms（改善 57%），但仍未達標。

### 根本原因：`store.save()` 仍在主要 Redis

問題在於，每個 worker 在完成 `deliver()` 後，必須呼叫 `store.save()` 把狀態寫回 Redis：

```python
async def _process_message(r, msg_id, data, loop):
    nid = data[b"notification_id"].decode()
    notification = await store.aget(nid)          # HGETALL → primary Redis
    await loop.run_in_executor(None, deliver, notification)
    await store.save(notification)                 # HSET+SET+ZADD → primary Redis ← 瓶頸！
    await r.xack(STREAM_KEY, GROUP_NAME, msg_id)  # → DELIVERY_REDIS_URL ✓
```

`store.save()` 在內部執行一個 3 指令的 pipeline：

```
pipeline(
    HSET notifications:{id} ...    ← 通知狀態
    SET notif:key:{hash} {id}      ← idempotency index
    ZADD user:{user_id}:notifs ... ← user 列表
)
```

### 壓力計算

```
Primary Redis 壓力來源（Tier 7）：

  API（~600 VUs）：
    INCR+EXPIRE（rate limit）× 600
    GET（idempotency）× 600
    pipeline(HSET+SET+ZADD)（save PENDING）× 600
    ← XADD 已移走到 delivery Redis ✓

  Workers（4 × BATCH_SIZE=20 = 80 concurrent）：
    HGETALL（store.aget）× 80
    pipeline(HSET+SET+ZADD)（store.save）× 80    ← 仍在！
    ← XREADGROUP/XACK 已移走到 delivery Redis ✓
```

我們移走了 Stream 操作（XADD/XREADGROUP/XACK），但 delivery status 的回寫（store.save × 80）仍然打在主要 Redis 上。這 80 個並發 pipeline 與 API 的讀寫持續競爭同一個 Redis command queue。

### 視覺化對比

```
Tier 6（1 Redis，所有操作）：
  Primary Redis：API reads + API writes + XADD + worker HGETALL + worker save + XREADGROUP + XACK
  負載：極高 → p95 POST 1,450ms ❌

Tier 7（2 Redis，Stream 隔離）：
  Primary Redis：API reads + API writes + worker HGETALL + worker save
  Delivery Redis：XADD + XREADGROUP + XACK
  Primary Redis 負載：中高 → p95 POST 623ms ❌（改善但未達標）

真正隔離（假設 Tier 7 完整版）：
  Primary Redis：API reads + API writes only
  Delivery Redis：XADD + XREADGROUP + XACK + worker HGETALL + worker save
  Primary Redis 負載：低 → p95 POST 預計 ~350ms ✓
```

---

## 如何真正隔離 Delivery 操作

要完全消除 worker 對主要 Redis 的壓力，delivery status write 也必須移出。以下是幾個選項：

### Option A：獨立 Delivery Status Redis（Redis-C）

```
Redis-A（API 主要）：idempotency + rate limit + user ZSET
Redis-B（Delivery Stream）：XADD / XREADGROUP / XACK / DLQ
Redis-C（Delivery Status）：worker 寫入 SENT/FAILED 狀態

GET /notifications/{id} 需要同時查詢 Redis-A 和 Redis-C 並合併結果
```

缺點：
- GET 路由邏輯變複雜（需要 fan-out 查詢）
- 3 個 Redis 實例的維運成本
- 若 Redis-C 不可用，delivery status 寫入失敗但 XACK 成功 → 狀態不一致

### Option B：Event/Callback 機制

delivery 完成後發送事件，API state 非同步更新。這是 event sourcing 的概念，在當前規模是過度設計。

### Option C：維持 Tier 6a 方案（最實際）

回到 `num_workers × BATCH_SIZE = constant` 的原則：

```
4 workers × BATCH_SIZE=5 = 20 concurrent（同 Tier 4 的負載）
```

Tier 6a 已驗證這個方案可以達標（POST p95 351ms），而且：
- 不需要額外的 Redis 實例
- 不需要複雜的 fan-out 查詢邏輯
- 維運複雜度低
- 4 個 worker 提供容錯能力（1 個掛掉剩 75% 容量，而非 0%）

---

## 向後相容性設計

`DELIVERY_REDIS_URL` 的 fallback 設計確保所有舊部署無縫相容：

```python
DELIVERY_REDIS_URL = os.getenv("DELIVERY_REDIS_URL", "") or REDIS_URL
```

| 部署情境 | `DELIVERY_REDIS_URL` 環境變數 | 實際使用的 Redis |
|---------|------------------------------|----------------|
| Tier 1–6a（舊部署） | 未設定 | `REDIS_URL`（單一 Redis） |
| Tier 7（新部署） | `redis://redis-delivery:6379/0` | 分離的 delivery Redis |

不需要任何 migration script，不需要修改舊的 `docker-compose.yml`，直接升級程式碼即可。

---

## 和其他 Tier 的比較

| Tier | 架構 | POST p95 | GET p95 | Throughput | 重點學習 |
|------|------|----------|---------|------------|---------|
| Tier 4 | 1 worker，async，BS=20 | 361ms ✓ | 172ms ✓ | 2,736 RPS | async gather baseline |
| Tier 6 | 4 workers，BS=20，1 Redis | 1,450ms ❌ | 532ms ❌ | 800 RPS | Redis 飽和 |
| Tier 6a | 4 workers，BS=5，1 Redis | 351ms ✓ | 162ms ✓ | 2,838 RPS | BATCH_SIZE 反向調整 |
| Tier 7 | 4 workers，BS=20，2 Redis | 623ms ❌ | 281ms ✓ | 1,963 RPS | Stream 隔離部分有效 |

---

## 關鍵學習

### 1. Redis 分離要看 workload 的實際 write pattern

只看 Stream 操作（XADD/XREADGROUP/XACK）就決定隔離哪個 Redis，是不完整的分析。必須追蹤每一個會打到 Redis 的操作，包括：
- API 的讀寫（idempotency、rate limit、state write）
- Worker 的讀寫（store.aget、store.save）
- Stream 操作（XADD、XREADGROUP、XACK）

隔離其中一類操作，但保留另一類在主要 Redis，只能解決部分問題。

### 2. 診斷順序：先量測，再決定

正確的診斷流程：

```bash
# 確認 Stream 積壓
redis-cli XINFO STREAM notifications:delivery

# 觀察 Redis 操作分布（用 MONITOR 短暫取樣）
redis-cli MONITOR | head -n 1000 | grep -E "XADD|XREADGROUP|HSET|ZADD" | sort | uniq -c | sort -rn

# Prometheus：查看 Redis 指令執行時間分布
redis_commands_duration_seconds_bucket{cmd="hset"}
redis_commands_duration_seconds_bucket{cmd="xreadgroup"}
```

應該先用工具確認哪些操作佔 Redis 時間最多，再決定如何拆分，而不是根據直覺猜測。

### 3. 向後相容設計：新 env var 設計有 fallback

在引入新的基礎設施元件時，應讓新設定有合理的預設值（fallback），避免現有部署被破壞。`DELIVERY_REDIS_URL` fallback 到 `REDIS_URL` 的設計，讓 Tier 7 的程式碼可以直接部署到舊環境而不需要任何設定變更。

### 4. Tier 6a 的 BATCH_SIZE 調整比 Tier 7 的 infrastructure 拆分更有效

在當前 RPS 規模（~2,000 RPS）下，控制並行數量比增加 infrastructure 更有效：
- Tier 6a：BATCH_SIZE=5，1 Redis，POST p95 = 351ms ✓
- Tier 7：BATCH_SIZE=20，2 Redis，POST p95 = 623ms ❌

增加一個 Redis 實例帶來的隔離效果，不如直接把並行數量降回安全範圍的效果顯著。

### 5. Multi-Redis 架構在 write 量更大時才真正發揮效果

理論上，分離 Redis 可以讓兩個工作負載各自獨立擴展。但這個優勢只有在 write 量大到單一 Redis 真的成為瓶頸時才會顯現。

在當前測試環境中：
- 主要 Redis 的壓力是 Stream 操作 + store.save，合計 ~80 個並發 pipeline
- 移走 Stream 操作後，store.save（~80 個）仍然飽和主要 Redis
- 真正「解壓」需要同時移走 stream 操作和 delivery status write

Multi-Redis 真正的價值在於：
- 主要 Redis 和 delivery Redis 可以分別設定 `maxmemory` 策略
- 各自設定不同的 persistence 模式（主要 Redis AOF；delivery Redis RDB 即可）
- 獨立的監控和告警，更容易定位問題
- 實例故障影響範圍隔離（delivery Redis 掛掉不影響 API 的 idempotency 和 rate limit）

---

## 結論

Tier 7 是一個方向正確但不完整的最佳化：

- **成功隔離**：Stream 操作（XADD/XREADGROUP/XACK）移到獨立 Redis ✓
- **未能隔離**：delivery status write（store.save × 80）仍在主要 Redis ✗
- **GET 達標**：p95 281ms < 500ms，因為主要 Redis 讀取壓力下降 ✓
- **POST 未達標**：p95 623ms > 500ms，因為 store.save 競爭未解決 ✗

在當前規模下，Tier 6a 的 `num_workers × BATCH_SIZE = constant` 原則更實用。Tier 7 的 Multi-Redis 架構在 delivery status write 量遠超過當前水準時（例如 10,000+ RPS）才值得投入額外的 infrastructure 複雜度。
