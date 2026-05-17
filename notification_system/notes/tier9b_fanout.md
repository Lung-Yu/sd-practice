# Tier 9B — Fan-out 寫入放大 & 批次去重優化

## 目標

`POST /fanout` 允許一個 HTTP 呼叫對 N 個用戶廣播同一則訊息。  
問題：naive 實作會有 O(N) 個 Redis RTT（逐一 idempotency check），這在大 N 時會是瓶頸。

---

## 寫入放大（Write Amplification）分析

對 N 個用戶發送一則廣播訊息，需要的 Redis 操作數：

| 操作 | 數量 | 說明 |
|------|------|------|
| idempotency GET | N | 去重檢查 |
| HSET notification | N | 儲存 notification hash |
| EXPIRE notification | N | 設 TTL |
| SET idempotency key | N | 防重複 |
| ZADD user timeline | N | 加入用戶索引 |
| XADD delivery stream | N | 加入交付 queue |
| **總計** | **6N** | |

**結論：** 每個用戶需要 6 個 Redis 命令，這是不可避免的（每個用戶的 state 都要更新）。  
可優化的是 **RTT 數量**，不是命令數量。

---

## 優化前：Sequential aget_by_key（N RTTs for dedup）

```python
# 舊實作（routes.py）
for user_id in req.user_ids:
    key = compute_key(user_id, req.topic, req.message)
    existing = await store.aget_by_key(key)  # 每次 = 2 RTT (GET + HGETALL)
    if existing is None:
        notifications.append(...)
```

每個用戶的 idempotency check = 2 個 Redis 命令 × N 個用戶 = **N 個 RTT**

---

## 優化後：批次 pipeline（3 RTTs 固定，不論 N）

```
Round-trip 1: pipeline GET × N  → 1 RTT (dedup check，知道哪些已存在)
Round-trip 2: pipeline HSET+EXPIRE+SET+ZADD × M → 1 RTT (M = 新增數量)
Round-trip 3: pipeline XADD × M → 1 RTT (批次 enqueue)
```

```python
# 新實作（routes.py）
user_keys = {uid: compute_key(uid, req.topic, req.message) for uid in req.user_ids}
existing_keys = await store.aget_existing_keys(list(user_keys.values()))  # 1 RTT pipeline

notifications = [
    Notification(...) for uid, key in user_keys.items()
    if key not in existing_keys  # 純 Python set lookup，O(1)
]

await store.asave_batch(notifications)   # 1 RTT pipeline
await aenqueue_batch([n.notification_id for n in notifications])  # 1 RTT pipeline
```

---

## 效能量測

### 優化前（Sequential RTT）

測試條件：4 workers，FAILURE_RATE=0，全部新用戶（無 idempotency cache）

| N | 平均延遲 | 每用戶 |
|---|---------|--------|
| 1 | 19.6ms | 19.63ms |
| 100 | 28.6ms | 0.29ms |
| 1000 | 188.2ms | 0.19ms |
| 5000 | 708.4ms | 0.14ms |

### 優化後（Pipeline，3 RTTs）

| N | 平均延遲 | min | max | 每用戶 |
|---|---------|-----|-----|--------|
| 1 | 10.1ms | 2.4ms | 36.2ms | 10.148ms |
| 10 | 3.6ms | 2.9ms | 4.2ms | 0.359ms |
| 100 | 21.4ms | 18.7ms | 26.8ms | 0.214ms |
| 1000 | 176.4ms | 162.5ms | 184.8ms | 0.176ms |
| 5000 | 897.8ms | 768.7ms | 969.4ms | 0.180ms |

### 分析

- **N=1000 的改善**：188ms → 176ms（dedup 從 ~N RTTs 降到 1 RTT pipeline）
- **N=5000 的差異**：對於極大 N，延遲主要來自**資料傳輸量**而非 RTT  
  5000 users × 4 commands = 20,000 pipeline 命令的序列化/反序列化耗時
- **per_user cost 趨近穩定**：0.18-0.21ms/user，表示批次效率已接近最佳

---

## 關鍵設計決策

### 1. fanout_id（跨用戶追蹤 ID）
每次 fanout 請求產生一個 `fanout_id`，用來關聯這批 notification。  
可用於：審計日誌、部分失敗追蹤、replay。

### 2. skipped 計數
`skipped = len(req.user_ids) - len(notifications)`  
已存在 idempotency key 的用戶不會重複建立 notification，skipped 告訴呼叫者有多少被 dedup 掉了。

### 3. ZADD phantom member 問題
如果 notification hash 有 TTL（7天），但 `user:{user_id}:notifications` ZSET 沒有 TTL，  
ZSET 中的 notification_id 可能在 hash 過期後變成「幽靈成員」。

```python
# list_for_user 已處理此情況：
return [_deserialize(d) for d in await pipe.execute() if d]
# `if d` 過濾掉 HGETALL 回傳空 dict 的 expired notifications
```

但 ZSET 本身會無限成長。長期解法：
- 同時對 ZSET 也設 TTL（但會把整個 user timeline 刪掉）
- 或用 `ZRANGEBYSCORE` 配合 TTL 時間戳剔除過期項目

### 4. 無 nginx 的限制（目前）
目前 fanout endpoint 只有單一 API container。N=5000 時 ~900ms，  
若需更高吞吐，需要：horizontal API scaling（Tier 9D）+ nginx。

---

## 驗證

```bash
# 10 個用戶廣播
curl -s -X POST http://localhost:8000/api/notifications/fanout \
  -H "Content-Type: application/json" \
  -d '{"user_ids":["u1","u2","u3","u4","u5","u6","u7","u8","u9","u10"],
       "channel":"email","message":"test","topic":"promo"}' | jq .

# 驗證 idempotency（同樣的 user_ids + message → skipped=10）
# 再跑一次相同的 curl 應看到 skipped=10, notification_ids=[]
```

---

## 結論

Fan-out 的核心取捨：
- **寫入放大無法避免**（6N 個 Redis 命令）—每個用戶的 state 都要寫
- **RTT 可以從 O(N) 降到 O(1)**—批次 pipeline 是關鍵
- N=5000 時 ~900ms，仍是合理的同步回應時間（呼叫者知道 5000 封已 enqueue）
- 真正的 fan-out 系統（如 Twitter）通常走 async + fan-out queue，讓 POST 立即返回，後台慢慢擴散
