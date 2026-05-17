# Tier 9C — Priority Queue（雙串流優先級排程）

## 目標

讓 `priority: "critical"` 的通知優先於 `priority: "normal"` 的通知被處理。  
例如：OTP 驗證碼 > 行銷推播。

---

## 設計：雙 Redis Stream

```
notifications:critical   → 高優先級（XADD 時路由）
notifications:delivery   → 一般優先級（原有 stream）
Consumer group: delivery-workers（兩個 stream 都用同一個 group）
```

---

## 路由邏輯

### 生產端（routes.py / queue.py）

```python
# queue.py
async def aenqueue(notification_id: str, priority: str = "normal") -> None:
    stream = STREAM_KEY_CRITICAL if priority == "critical" else STREAM_KEY
    await _get_async_client().xadd(stream, {"notification_id": notification_id})
```

`POST /send` 和 `POST /fanout` 都支援 `priority` 欄位，預設 `"normal"`。

### 消費端（worker.py）

```python
# 優先檢查 critical stream（非阻塞）
critical_msgs = await r.xreadgroup(GROUP_NAME, CONSUMER_NAME,
                                    {STREAM_KEY_CRITICAL: ">"}, count=BATCH_SIZE)
if critical_msgs:
    # 處理 critical
    active_stream = STREAM_KEY_CRITICAL
else:
    # 沒有 critical，阻塞等 normal（最多 BLOCK_MS）
    normal_msgs = await r.xreadgroup(GROUP_NAME, CONSUMER_NAME,
                                      {STREAM_KEY: ">"}, count=BATCH_SIZE, block=BLOCK_MS)
    active_stream = STREAM_KEY
```

**關鍵設計決策：non-blocking critical check + blocking normal fallback**
- critical 用非阻塞（`block` 不傳）：有就處理，沒有立刻跳到 normal
- normal 用阻塞（`block=BLOCK_MS`）：沒有 normal 訊息時 idle 等待，不佔 CPU

---

## XACK 的 stream_key 問題

XACK 必須指定正確的 stream。訊息從哪個 stream 讀出，就要 XACK 那個 stream：

```python
# _process_batch 新增 stream_key 參數
await r.xack(stream_key, GROUP_NAME, *msg_ids)
# 不是永遠用 STREAM_KEY！
```

容易忽略的 bug：把所有 XACK 都打到 `notifications:delivery`，那麼 critical 的訊息永遠不會被 ACK，最終累積在 PEL（Pending Entry List）。

---

## 驗證結果

### Stream 路由正確性
```
發送 3 normal + 2 critical：
  notifications:delivery  +3 ✓
  notifications:critical  +2 ✓
```

### Worker log 確認
```
[worker] 6bb297b4ca57 ready — consuming notifications:critical (priority)
then notifications:delivery / group=delivery-workers
```

### 優先級效果（低負載）
```
Critical (n=5): avg=0.02s  max=0.11s
Normal   (n=78): p50≈0.00s (idle workers, both drain instantly)
```

低負載時，兩者都快（workers 閒置，馬上消費）。  
**優先級在「持續高負載 + normal 有積壓」時才明顯**。

### 高負載下的理論行為
```
t=0: 80 normal enqueued (workers processing round 1)
t=0.1s: 5 critical enqueued
t=0.2s: round 1 done → next poll checks critical first → critical delivered
t=0.2s: normal messages start round 2

Critical delivered at ~0.2s, normal p50 at ~0.3-0.5s (depending on batch size)
```

---

## 與其他實作比較

| 方案 | 優點 | 缺點 |
|------|------|------|
| **雙 Stream（本方案）** | 嚴格優先、低延遲、可擴充多個 priority level | 每個 priority level 需要一個 stream + group |
| **單 Stream + Score 排序** | 只要一個 stream | Redis Stream 不支援按分數讀取，要自行排序 |
| **Redis Sorted Set** | 天然優先級（ZADD score） | 無 consumer group 語義，需自行實作 ACK + 重試 |
| **Celery priority queue** | 成熟實作 | 需要 Celery broker，增加系統複雜度 |

---

## 生產建議

1. **Priority levels 不要超過 3 個**（critical / high / normal）— 太多層級增加運維複雜度
2. **Critical stream 要監控積壓**：`XLEN notifications:critical > 100` → alert
3. **Critical stream 要有 DLQ**：失敗的 critical 訊息比 normal 更需要保留和重試
4. **Consumer group 共用**：critical 和 normal 使用同一個 group（delivery-workers），確保 worker 可以同時消費兩個 stream，不需要專屬 critical worker

---

## 檔案變更清單

| 檔案 | 變更 |
|------|------|
| `queue.py` | 新增 `STREAM_KEY_CRITICAL`；`ensure_group()` 在兩個 stream 建立 group；`enqueue/aenqueue/aenqueue_batch` 接受 `priority` 參數 |
| `schemas.py` | `SendRequest` 和 `FanoutRequest` 新增 `priority: str = "normal"` |
| `routes.py` | 傳遞 `priority=req.priority` 給 `aenqueue/aenqueue_batch` |
| `worker.py` | `_ensure_group` 在兩個 stream 建立 group；`_process_batch` 新增 `stream_key` 參數；主迴圈改為 critical-first 雙 stream 輪詢 |
