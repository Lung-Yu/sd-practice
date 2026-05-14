# Tier 2A：Redis 共享狀態層

**實施日期：** 2026-05-14

## 改動摘要

將原本的 in-memory `NotificationStore`（module-level singleton，`threading.Lock` + 三個 dict）替換為 Redis 後端的 `RedisNotificationStore`。引入 `store.py` 工廠函數 `_make_store()`，根據環境變數 `REDIS_URL` 決定返回哪種實作，向上層路由完全透明。

### Redis 資料模型

| Key 格式 | 型別 | 說明 | TTL |
|----------|------|------|-----|
| `notification:{id}` | HASH | 所有欄位（user_id, topic, message, status, ...） | 無 |
| `idempotency:{sha256_key}` | STRING | 對應的 notification_id | 24h |
| `user:{user_id}:notifications` | ZSET | score = created_at unix timestamp | 無 |

### 核心實作模式

**`store.py` 工廠（向後相容）：**
```python
def _make_store():
    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        return RedisNotificationStore(redis_url)
    return NotificationStore()   # 原本 in-memory 實作不變

_store = _make_store()
```

**`store_redis.py` — `save()` 用 pipeline 一次 round-trip：**
```python
def save(self, notification: Notification) -> None:
    pipe = self._r.pipeline()
    pipe.hset(f"notification:{notification.id}", mapping=asdict(notification))
    pipe.set(f"idempotency:{notification.idempotency_key}",
             notification.id, ex=86400)
    pipe.zadd(f"user:{notification.user_id}:notifications",
              {notification.id: notification.created_at})
    pipe.execute()   # 單次 TCP 往返
```

**`list_for_user()` — ZRANGE 取 ID 清單，再批次 HGETALL：**
```python
def list_for_user(self, user_id: str) -> list[Notification]:
    ids = self._r.zrange(f"user:{user_id}:notifications", 0, -1)
    if not ids:
        return []
    pipe = self._r.pipeline()
    for nid in ids:
        pipe.hgetall(f"notification:{nid}")
    results = pipe.execute()
    return [Notification(**r) for r in results if r]
```

Redis 以 `--appendonly yes`（AOF 模式）啟動，確保 crash 後資料不遺失。

## k6 負載測試結果（Tier 2A）

**測試條件：** 4 uvicorn workers、FAILURE_RATE=0、target 5000 RPS、600 VU 上限、k6 ramping-arrival-rate

| 指標 | 結果 | 目標 | 通過？ |
|------|------|------|--------|
| POST /send p95 | 544ms | < 500ms | ❌ |
| POST /send p99 | 738ms | < 1000ms | ✓ |
| GET /{id} p95 | 462ms | < 500ms | ✓ |
| GET list p95 | 466ms | < 500ms | ✓ |
| 錯誤率 | 0.00% | < 1% | ✓ |
| Cross-worker 404s | 0 | — | ✓（Redis 修復） |
| 吞吐量 | ~1750 RPS | 5000 RPS | ❌ |
| Dropped iterations | 196,653 | 0 | ❌ |

## Little's Law 分析

Little's Law 公式：

```
L = λ × W
```

- `L`：系統中同時存在的請求數（VU 上限 = 600）
- `λ`：吞吐量（requests/sec）
- `W`：平均延遲（seconds）

### 推算理論最大吞吐量

測試期間 POST /send 平均延遲約 **244ms**：

```
λ_max = L / W = 600 VU / 0.244s ≈ 2459 RPS（理論上限）
```

然而實測只有 **1750 RPS**，低於理論上限的原因：

- 系統在 600 VU 壓力下已開始排隊（queueing），延遲不是固定值，而是隨負載攀升
- 排隊延遲讓平均 W 持續上升 → λ 下降 → 正反饋惡化
- k6 的 `ramping-arrival-rate` 在 VU 耗盡時自動丟棄新到達的 iteration，形成 196,653 次 dropped iterations

要達到 5000 RPS，在 244ms 平均延遲下需要：

```
L_needed = 5000 × 0.244 = 1220 VUs
```

k6 上限 600 VU 不夠，即使完全沒有 queueing 也只能到 ~2459 RPS。**瓶頸不在 Redis，在 VU 數量與延遲的乘積。**

## POST /send p95 > 500ms 的根本原因

FAILURE_RATE=0 時，同步交付路徑仍需要 **2 次 Redis round-trip**：

```
POST /send
  ├─ 1️⃣  Redis pipeline: HSET(PENDING) + SET(idempotency) + ZADD
  ├─  channel.send()  ← 即使是 stdout，也是函數呼叫 + GIL 爭搶
  └─ 2️⃣  Redis pipeline: HSET(SENT)
```

每次 Redis 往返在本機 Docker 網路約 0.5–2ms，但 4 個 uvicorn worker 共用同一個 Redis 連線池，在 1750 RPS 壓力下連線排隊疊加成數十 ms。兩次 round-trip 累積，加上 Python GIL 在多執行緒切換時的開銷，p95 突破 500ms。

## Redis 修復了什麼 vs. in-memory

| 問題 | In-memory（Tier 1） | Redis（Tier 2A） |
|------|---------------------|-----------------|
| Cross-worker GET 404 | 頻繁（每個 worker 獨立 dict） | 完全消除 |
| 跨 worker 冪等性（idempotency） | 無（同請求可能被兩個 worker 各執行一次） | 全域去重 |
| 持久性 | 程序重啟即清空 | AOF 保障 crash 存活 |
| 記憶體上限 | 無限成長（idempotency dict 永不清理） | TTL 24h 自動過期 |
| 可水平擴展 | 不可（狀態不共享） | 可（所有 worker 共享同一 Redis） |

## 結論

Tier 2A 完成了最核心的架構升級：從「各自為政的 in-memory dict」變為「單一 Redis 共享狀態」，解決了多 worker 場景下最致命的正確性問題（cross-worker 404、跨 worker idempotency 失效）。

效能上，吞吐量瓶頸來自 Little's Law 的 VU 上限與同步交付路徑的雙重 Redis round-trip，並非 Redis 本身是問題所在。**下一步 Tier 2B** 嘗試用 FastAPI `BackgroundTasks` 將 `deliver()` 移出 HTTP 路徑，以消除 POST /send 的第二次 Redis 往返——但結果揭示了一個反直覺的陷阱。
