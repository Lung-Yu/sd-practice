# Tier 7+：批次讀取與批次 ACK 優化

## 背景

Tier 7 已把 Stream/DLQ 移到獨立的 delivery Redis，save_status() 也從 3 命令 pipeline 縮成 1 個 HSET。但 POST p95 在 4 worker × BS=5 條件下仍約 564ms（之前測試，primary Redis 有大量歷史資料）。

本 Tier 針對兩個剩餘的 Redis 往返開銷再優化：

1. **`abatch_get()`**：把 N 個獨立的 HGETALL 合成一個 pipeline round-trip
2. **Batch XACK**：把 N 個獨立的 XACK 合成一個命令

---

## 問題分析

### 舊版 `_process_message`（每訊息一個 coroutine）

```
for each message in batch (via asyncio.gather):
    await store.aget(nid)   → 1 HGETALL round-trip to primary Redis
    run_in_executor(deliver) → ThreadPool
    await r.xack(...)        → 1 XACK to delivery Redis
```

BS=5 時：
- 5 個並發 HGETALL（各自一個 round-trip）= 5 round-trips（可能部分重疊）
- 5 個獨立 XACK = 5 round-trips

### 新版 `_process_batch`（批次合併）

```
await store.abatch_get([nid1, nid2, ...])  → 1 pipeline round-trip（N 個 HGETALL）
asyncio.gather(*deliver_tasks)              → N 個並發 deliver()
await r.xack(STREAM_KEY, GROUP_NAME, *msg_ids)  → 1 XACK command
```

BS=5 時：
- 1 pipeline（5 個 HGETALL）= 1 round-trip
- 1 XACK（5 個 msg_id）= 1 round-trip

---

## 程式碼變更

### `store_redis.py`：新增 `abatch_get()`

```python
async def abatch_get(self, notification_ids: list[str]) -> list[Optional["Notification"]]:
    """Fetch N notifications in one pipeline round-trip instead of N individual HGETALLs."""
    if not notification_ids:
        return []
    pipe = self._ar.pipeline()
    for nid in notification_ids:
        pipe.hgetall(f"notification:{nid}")
    results = await pipe.execute()
    return [_deserialize(d) if d else None for d in results]
```

### `worker.py`：`_process_batch()` 取代 `_process_message()`

```python
async def _process_batch(r, msgs, loop):
    msg_ids = [msg_id for msg_id, _ in msgs]
    nids = [data.get("notification_id") for _, data in msgs]

    # 1 pipeline → N HGETALLs（vs N 個獨立 round-trips）
    notifications = await store.abatch_get([nid for nid in nids if nid])

    nid_list = [nid for nid in nids if nid]
    nid_to_notif = {nid: n for nid, n in zip(nid_list, notifications) if n is not None}

    # 並發 deliver
    deliver_tasks = []
    for nid in nids:
        notif = nid_to_notif.get(nid) if nid else None
        if notif is not None:
            deliver_tasks.append(loop.run_in_executor(None, deliver, notif))
        else:
            deliver_tasks.append(asyncio.sleep(0))
    results = await asyncio.gather(*deliver_tasks, return_exceptions=True)

    # 1 XACK 命令（vs N 個獨立 XACK）
    if msg_ids:
        await r.xack(STREAM_KEY, GROUP_NAME, *msg_ids)
```

---

## 啟動競態條件修復

**問題**：`_wait_for_redis(r)` 只等待 delivery Redis。若 primary Redis 還在 AOF replay，
`abatch_get()` 立即失敗，worker 進入緊湊錯誤迴圈，每隔 ~8ms 一次錯誤，並且
已讀取的訊息都不會被 ACK，造成大量 pending 訊息堆積。

**根本原因**：Redis 7 在 AOF loading 期間 PING 仍然回 PONG，但資料命令回傳
`LOADING` 錯誤。`_wait_for_redis` 用 PING 判斷 Redis 是否就緒，所以判斷錯誤。

**修復**：在 `run()` 中同時等待兩個 Redis：

```python
async def run():
    r = aioredis.from_url(config.DELIVERY_REDIS_URL, ...)
    await _wait_for_redis(r)   # delivery Redis

    # 同時等待 primary Redis（abatch_get 使用）
    primary_r = aioredis.from_url(config.REDIS_URL, decode_responses=True)
    await _wait_for_redis(primary_r)
    await primary_r.aclose()

    await _ensure_group(r)
```

---

## 基準測試結果

**設定**：4 worker × BS=5, 2 Redis（primary + delivery），FAILURE_RATE=0, k6 @5000 RPS

### 對比表

| 版本 | POST p95 | GET p95 | LIST p95 | 通過? |
|------|----------|---------|----------|-------|
| Tier 4（1w × BS=20, 1 Redis）| 361ms | - | - | ✗ |
| Tier 6（4w × BS=20, 1 Redis）| 1450ms | - | - | ✗ |
| Tier 6a（4w × BS=5, 1 Redis）| 351ms | - | - | ✓ |
| Tier 7+save_status（前次測試，舊資料）| 564ms | - | - | ✗ |
| Tier 7+save_status（4M keys 污染後）| 759ms | - | - | ✗ |
| **Tier 7++abatch_get（清空 Redis）** | **408–430ms** | **143–153ms** | **210–224ms** | **✓** |

### 最終測試結果（clean Redis）

```
post_send_duration:     avg=331ms   p(95)=430ms   p(99)=520ms
get_by_id_duration:     avg=111ms   p(95)=153ms   p(99)=267ms
list_by_user_duration:  avg=165ms   p(95)=224ms   p(99)=379ms
http_req_failed:        0.00%
iterations:             178,461
```

---

## 關鍵發現：Redis 資料量對延遲的影響

**現象**：Primary Redis 從空開始 → 約 408ms POST p95。  
累積 4M 個 key（數次測試後）→ 759ms POST p95，超過閾值。

**原因**：
- Redis 雖然全部在 RAM，但 4M keys 需要更多記憶體尋址
- Container 記憶體壓力可能導致 swap
- 大量 AOF 重放時 data commands 被封鎖（`LOADING` 錯誤）
- Redis 單執行緒在處理更大 keyspace 時，每個命令耗時微增但累計明顯

**生產解決方案**：
1. 通知 HASH 設 TTL（例如 7 天）：`EXPIRE notification:{id} 604800`
2. 設定 Redis maxmemory + LRU 淘汰策略：`maxmemory-policy allkeys-lru`
3. 定期 FLUSHDB 或 backup-and-restore 控制 keyspace 大小
4. 分離讀取 Redis（readonly replica）

---

## abatch_get 的設計取捨

### 優點：減少 round-trips
- N 個 HGETALL → 1 pipeline = 1 TCP round-trip（節省 (N-1) × RTT）

### 潛在缺點：back-pressure 增加
- 舊方法：5 個 coroutine 並發，第 1 個 HGETALL 完成 → 立即開始 deliver()，
  與其他 HGETALL 並行。delivery 和 reads 可重疊。
- 新方法：等所有 5 個 HGETALL 完成 → 才開始所有 deliver()。
  讀取和遞送不重疊。

實際上因為 BS=5 且 Redis 讀取只需 ~1ms，重疊帶來的好處極小，
而 pipeline 省下的 4 個 RTT 更顯著。

### 適合 abatch_get 的場景
- BS 較大（BS=20 可省下 19 個 RTT）
- Redis latency 較高（跨機房或高負載）
- 讀取不是 critical path 的瓶頸

---

## Batch XACK 的正確性

```python
await r.xack(STREAM_KEY, GROUP_NAME, *msg_ids)
```

`XACK` 支援多個 message ID：`XACK key group id [id ...]`。
整批一次 ACK，語義與逐一 ACK 完全相同。

**注意**：任何 msg_id 無論對應通知是否存在，都必須 ACK，
否則 pending 清單持續累積，consumer group 膨脹。

---

## 總結

| 優化 | 位置 | 效果 |
|------|------|------|
| `save_status()` | `delivery.py` + `store_redis.py` | 3 commands → 1 HSET per delivery |
| `abatch_get()` | `worker.py` + `store_redis.py` | N HGETALLs → 1 pipeline per batch |
| Batch XACK | `worker.py` | N XACKs → 1 command per batch |
| 2 Redis 分離 | `docker-compose.yml` | Stream 不與 API state 競爭 |
| 等待 primary Redis 啟動 | `worker.py` | 消除啟動競態條件 |
| Redis keyspace 管理 | （未實作，未來 TODO）| 防止 4M key 性能退化 |
