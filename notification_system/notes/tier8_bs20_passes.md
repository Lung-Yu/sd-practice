# Tier 8：BS=20 突破 500ms 門檻

## 目標

Tier 6 時，4w × BS=20 POST p95=1,450ms，嚴重超標。
Tier 7+ 加入 abatch_get + batch XACK + save_status + 2 Redis 後，能否讓 BS=20 重新通過 500ms？

答案：**是。**

---

## 配置

| 項目 | 設定 |
|------|------|
| delivery worker | 4 個 |
| BATCH_SIZE | 20 |
| FAILURE_RATE | 0 |
| Redis | 2 個（primary + delivery 分離）|
| 優化 | abatch_get + batch XACK + save_status |
| Redis keyspace | 清空（fresh start） |

---

## 最終測試結果

```
post_send_duration:     avg=329ms   p(95)=433ms  p(99)=477ms  ✓
get_by_id_duration:     avg=110ms   p(95)=157ms  p(99)=263ms  ✓
list_by_user_duration:  avg=164ms   p(95)=226ms  p(99)=373ms  ✓
http_req_failed:        0.00%                                  ✓
notification_error_rate: 0.00%                                 ✓
checks_succeeded:       100.00% (356,588/356,588)              ✓
```

---

## BS=20 完整演進

| Tier | 設定 | POST p95 | 通過? | 問題 |
|------|------|----------|-------|------|
| T6 | 4w × BS=20, 1 Redis | 1,450ms | ❌ | 80 concurrent pipeline 把 Redis 打爆 |
| T7 | 4w × BS=20, 2 Redis | 623ms | ❌ | Worker 仍 80 concurrent save() 打 primary Redis |
| T7+save_status (dirty) | 4w × BS=20 | 821ms | ❌ | 4M keys 導致基準線退化 |
| **T8 (all opts, clean)** | **4w × BS=20** | **433ms** | **✓** | — |

---

## 為什麼 BS=20 現在能過？

### 原來 Tier 6 的問題

每個訊息觸發獨立的 Redis 命令：

```
per message (80 concurrent):
  HGETALL notification:{id}    → 1 round-trip × 80 = 80 round-trips to primary Redis
  HSET+SET+ZADD pipeline       → 3 commands × 80 = 240 commands to primary Redis
  XACK                         → 1 × 80 = 80 commands to delivery Redis
```

**Total**: 80 concurrent HGETALL round-trips + 240 pipeline commands/cycle。

### Tier 8 的命令數

```
per batch (4 workers × 1 batch each):
  abatch_get([20 nids])       → 1 pipeline with 20 HGETALLs × 4 = 4 round-trips, 80 HGETALL
  save_status() per delivery  → 1 HSET × 80 = 80 HSETs to primary Redis
  batch XACK(*20 msg_ids)     → 1 XACK × 4 = 4 commands to delivery Redis
```

**Total**: 4 round-trips（代替 80）+ 80 HGETs + 80 HGETs（abatch） + 80 HGETs（save_status）

**關鍵差異：**

| 操作 | Tier 6 | Tier 8 | 改善 |
|------|--------|--------|------|
| HGETALL round-trips | 80 | 4 pipeline | −95% |
| delivery-side writes | 80 HSET+SET+ZADD | 80 HSET only | −67% commands |
| XACK commands | 80 | 4 | −95% |

Redis 是單執行緒。減少命令數 = 減少 Redis 排隊時間 = 降低 API 等待 Redis 的時間。

---

## 發現：setup() EOF 競態條件

第一次 BS=20 測試（clean Redis, 舊容器未重建）出現 37,076 個 404：

```
notification_404_count: 37076
http_req_failed: 20.08%
```

**根本原因**：k6 `setup()` 在容器重啟後立即發送 200 個 POST 請求。
此時 uvicorn 正在重新建立 Redis 連線池，部分請求收到 EOF（TCP 連線被關閉）。
`setup()` 收到非 2xx 回應，不把 notification_id 加入 seedIds。
結果：`seedIds.length === 0` → 所有 GET 請求使用 fallback UUID → 全部 404。

**修復方式**：

1. 等待容器完全啟動後再執行 k6（建議 8-10 秒）
2. 或在 k6 setup() 加入重試邏輯

k6 腳本現有的設計：
```javascript
if (res.status === 404) {
  notFoundCount.add(1);
  return; // 不算 error，但也不記錄到 errorRate
}
```
這是為了容忍 in-memory 多 worker 的 cross-worker 404。
用 Redis 後這個情況不應發生，404 代表真正的問題。

---

## 啟動競態條件修復（worker.py）

問題：primary Redis 在 AOF replay 時，PING 回 PONG，但資料命令回 LOADING 錯誤。
舊 worker 只等待 delivery Redis，直接進入主迴圈，abatch_get() 觸發 LOADING → 緊湊錯誤迴圈。

```python
# 新增：同時等待 primary Redis
primary_r = aioredis.from_url(config.REDIS_URL, decode_responses=True)
await _wait_for_redis(primary_r)
await primary_r.aclose()
```

重建容器後，日誌顯示：
```
[worker] Redis not ready (Redis is loading the dataset in memory), retry 1/60…
[worker] metrics server listening on :8001
[worker] {container_id} ready — consuming notifications:delivery/delivery-workers
```

Primary Redis 被明確等待後才開始處理訊息。

---

## 效能演進完整圖

```
Tier 4  (1w × BS=20, 1 Redis)            POST p95 = 361ms  ✓
Tier 6  (4w × BS=20, 1 Redis)            POST p95 = 1450ms ❌  ← Redis 飽和
Tier 6a (4w × BS=5,  1 Redis)            POST p95 = 351ms  ✓   ← 降 BS 解決
Tier 7  (4w × BS=20, 2 Redis)            POST p95 = 623ms  ❌  ← save() 仍打 primary
Tier 7+ (4w × BS=5,  2 Redis, abatch)    POST p95 = 430ms  ✓
Tier 8  (4w × BS=20, 2 Redis, all opts)  POST p95 = 433ms  ✓   ← BS 回 20，仍通過！
```

Tier 8 的意義：用同樣的硬體（4 worker）處理 4× 的批次大小（BS=5→20），
延遲保持相同（430ms vs 433ms）。理論上這讓 worker 有更大的彈性空間應對突發流量。

---

## 小結

| 問題 | 解法 | 效果 |
|------|------|------|
| BS=20 打爆 Redis | abatch_get：N round-trips → 1 pipeline | 95% round-trip 減少 |
| 3-command delivery write | save_status：3 → 1 HSET | 67% command 減少 |
| N 個獨立 XACK | batch XACK：N → 1 command | 95% command 減少 |
| Worker/API 競爭 primary Redis | 2 Redis 分離 | Stream 壓力與 API state 隔離 |
| Worker 啟動競態 | 等待兩個 Redis | 消除 LOADING 錯誤迴圈 |
| keyspace 污染退化 | 清空 Redis | 基準線穩定在 430ms |
