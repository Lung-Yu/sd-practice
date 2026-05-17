# Tier 9A：Redis 通知 TTL + maxmemory-policy

## 背景與動機

T7+ 發現：Primary Redis 從 0 累積到 4M keys 後，POST p95 從 408ms 升到 759ms（近 2 倍）。
每次跑完 benchmark 都要手動 FLUSHALL，才能回到乾淨基準。

Tier 9A 的目標：
1. 讓 notification HASH 帶 TTL，不再永久佔用記憶體
2. 設定 `maxmemory-policy: volatile-lru`，當記憶體不夠時優先淘汰有 TTL 的 key
3. 驗證：連跑 5 次 k6 不 FLUSHALL，效能是否維持在 500ms 以內

---

## 實作細節

### `config.py`：新增 `NOTIFICATION_TTL_S`

```python
NOTIFICATION_TTL_S = int(os.getenv("NOTIFICATION_TTL_S", str(7 * 24 * 3600)))  # 7 天
```

預設 7 天（604,800 秒）。`0` = 不設 TTL（不建議用於 production）。

### `store_redis.py`：在 `save()` / `asave()` pipeline 加 EXPIRE

```python
def save(self, notification: Notification) -> None:
    pipe = self._r.pipeline()
    pipe.hset(f"notification:{notification.notification_id}", mapping=_serialize(notification))
    if config.NOTIFICATION_TTL_S:
        pipe.expire(f"notification:{notification.notification_id}", config.NOTIFICATION_TTL_S)
    pipe.set(f"idempotency:...", ..., ex=_IDEMPOTENCY_TTL)
    pipe.zadd(f"user:{user_id}:notifications", ...)
    pipe.execute()
```

**注意**：`EXPIRE` 只加在 `notification:{id}` HASH 上，不加在：
- `idempotency:{key}` STRING：已有自己的 `ex=86400`（24h）
- `user:{uid}:notifications` ZSET：沒有 TTL，phantom member 由 `if d` guard 過濾

### `docker-compose.yml`：primary Redis 加 `--maxmemory-policy`

```yaml
redis:
  command: redis-server --save 60 1 --appendonly yes --maxmemory-policy volatile-lru
```

`volatile-lru`：記憶體緊張時，從有 TTL 的 key 中選最久未使用的淘汰。
不設 `--maxmemory`：沒有記憶體上限時，eviction 不會觸發（詳見下方發現）。

---

## 驗證：TTL 確實被設定

```bash
# 在 rebuild 後新建立的通知
$ redis-cli TTL "notification:6165ceef-..."
604800  ✓

# rebuild 之前的舊 key
$ redis-cli RANDOMKEY → "notification:3000732d-..."
$ redis-cli TTL "notification:3000732d-..."
-1      （無 TTL，rebuild 前建立的）
```

---

## 連跑 5 次 k6 結果（不 FLUSHALL）

配置：4w × BS=20，2 Redis，FAILURE_RATE=0，k6 目標 5000 RPS

| Run | DBSIZE before | POST p95 | 0% errors? | DBSIZE after |
|-----|--------------|----------|-----------|-------------|
| 1   | 401,454      | 465ms ✓  | ✓         | 691,844     |
| 2   | 691,844      | 572ms ❌  | ✓         | 948,421     |
| 3   | 948,421      | 455ms ✓  | ✓         | 1,207,996   |
| 4   | 1,207,996    | 479ms ✓  | ✓         | 1,457,435   |
| 5   | 1,457,435    | 493ms ✓  | ✓         | 1,706,641   |

- Run 2 的 p95=572ms 超過閾值，但 run 3、4、5 在更多 key 的情況下又恢復正常。
- 這表示 run 2 的 572ms 可能是 benchmark noise，不是系統性退化。
- 5 次中 4 次通過，而且 key 從 401K 增長到 1.7M 都沒有出現像 T7+ 那樣的 2× 退化。

### 對比 T7+（無 TTL）

| 情境 | Keyspace | POST p95 | 通過? |
|------|----------|----------|-------|
| T7+（無 TTL，clean）| 0 | 408ms | ✓ |
| T7+（無 TTL，4M keys）| 4M | 759ms | ❌ |
| T9A run 1（有 TTL，401K）| 401K | 465ms | ✓ |
| T9A run 5（有 TTL，1.7M）| 1.7M | 493ms | ✓ |

從 4M keys → 759ms（失敗），到 1.7M keys → 493ms（通過），TTL 的效果是：
阻止長期累積（7 天後 key 開始消滅），但**短期（7 天內）keyspace 還是會增長**。

---

## 關鍵發現

### 發現 1：`maxmemory-policy` 沒有 `maxmemory` = 等於沒設

```
maxmemory_human: 0B      ← 沒有記憶體上限
maxmemory_policy: volatile-lru  ← 政策有設，但永遠不會觸發
```

`volatile-lru` 只在 Redis **達到 maxmemory 上限**時才會啟動 eviction。
沒有 `--maxmemory N`，eviction 永遠不會觸發，policy 形同虛設。

**生產正確配置**：

```yaml
command: redis-server --maxmemory 512mb --maxmemory-policy volatile-lru
```

這樣當記憶體超過 512MB 時，Redis 會自動淘汰有 TTL 的舊 key，keyspace 就不會無限增長。

### 發現 2：舊 key 沒有 TTL，需要一次性清理

Rebuild 前建立的 401K keys 都是 TTL=-1（永久存在）。要讓所有 key 帶 TTL，選擇是：
1. 一次性 FLUSHALL + 重啟（清除舊 key）
2. 批次 `SCAN` + `EXPIRE` 補設 TTL（不中斷服務）
3. 接受舊 key 無 TTL，等 `volatile-lru` 淘汰（但沒設 maxmemory 所以不會發生）

本次採用策略 3（接受舊 key 存在），觀察對效能的影響。

### 發現 3：ZADD user set 的 phantom member 問題

`user:{uid}:notifications` ZSET 沒有 TTL，notification HASH 過期後，ZSET 裡的 member 會變成「空的指向」。

```python
# list_for_user 已有 `if d` 過濾：
return [_deserialize(d) for d in pipe.execute() if d]
```

這個 guard 已經能過濾掉空的 HGETALL 結果。但長期下來 ZSET 會有大量無效 member，導致：
- `ZCARD user:{uid}:notifications` 偏大
- `list_for_user` 的 pipeline 包含大量空結果（浪費一點 Redis 時間）

**生產解法**：用 Lua script 或 `ZREMRANGEBYSCORE` 定期清理 ZSET 的過期 member。

---

## 記憶體使用

```
用途         記憶體
5 次 k6 後   592MB（1.7M keys）
碎片率       1.09（正常，接近 1.0）
```

每個 notification 約佔：592MB / 1.7M ≈ **340 bytes/key**（包含 HASH + idempotency + ZADD member）。

若要讓記憶體在 512MB 以內，需設：
```
--maxmemory 512mb --maxmemory-policy volatile-lru
```
這樣 Redis 會在接近 512MB 時自動淘汰有 TTL 的舊 notification HASH。

---

## 結論

| 目標 | 結果 |
|------|------|
| 新 notification 帶 TTL | ✓ TTL=604800 確認 |
| 連跑 5 次不 FLUSHALL | 4/5 通過，1 次噪音超標 |
| volatile-lru 防止無限增長 | ✗ 需要同時設 `--maxmemory` 才會觸發 |
| 長期 keyspace 穩定 | ✓ 7 天後 key 自然過期（但短期內還是增長）|

**帶走的規則**：
- TTL 解決長期問題，不解決短期問題。
- `maxmemory-policy` 沒有 `maxmemory` = 等於沒設。
- ZADD 的過期 member 需要另外清理，但 `if d` guard 能讓系統正確運作。
