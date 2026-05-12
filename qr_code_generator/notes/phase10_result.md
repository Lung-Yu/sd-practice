# Phase 10 — LB 層驗證：直連壓測 + HAProxy 實驗 + Rate Limit 遷移（2026-05-12）

## 目標

Phase 9 的 redirect throughput 為 2,661 req/s，距離 5,000 QPS 目標仍有差距。
本 Phase 以三步驗證：

1. 直連 app1（繞過 Nginx），確認瓶頸是否在 LB 層
2. 將 Nginx 替換為 HAProxy，嘗試降低 LB 層 CPU 開銷
3. 將 rate limiting 從 Nginx 遷移至 App 層（FastAPI dependency）

---

## Stage 10a — 直連 app1 壓測（繞過 Nginx）

### 方法

暫時在 docker-compose.yml 為 app1 加上 `ports: "8001:8000"`，
以 `BASE_URL=http://localhost:8001` 直接跑 `redirect_only_test.js`，
排除 Nginx 的影響，測量單一 app container 的真實 ceiling。

### 結果

| 指標 | 直連 app1 | Nginx + 2 apps（Phase 9） |
|------|----------|--------------------------|
| throughput | **2,304 req/s** | 2,661 req/s |
| 錯誤率 | 0% | 0% |
| p50 | 84ms | — |
| p95 | 2.27s | — |

### 結論：瓶頸在 Python workers，不在 Nginx

理論上 2 個 app 各 2,304 req/s，加起來應達 4,608 req/s。
但 Nginx + 2 apps 實測只有 2,661 req/s，差了 42%。

根本原因：Podman VM 只有 8 vCPU，Nginx 8 workers + app1 6 workers + app2 6 workers = 20 個進程搶 8 核，
Nginx 本身吃掉了可觀的 CPU，但更根本的瓶頸是 Python workers 的 CPU 上限。
換更快的 LB 無法突破 Python workers 的天花板。

---

## Stage 10b — HAProxy L7 Load Balancer 實驗

### 假說

HAProxy 的 per-request CPU 開銷比 Nginx 低，可以把節省的 CPU 還給 Python workers。

### 變更

| 檔案 | 變更 |
|------|------|
| `docker-compose.yml` | nginx service → haproxy service（`haproxy:lts-alpine`，port 8100:8080） |
| `haproxy/haproxy.cfg` | 新建；HTTP mode，roundrobin，forwardfor，http-keep-alive，http-reuse always |
| `scaffold/app/main.py` | 加 `@app.middleware("http")` rate limit（後來改掉） |
| `scaffold/app/cache.py` | 加 `check_rate_limit()` Redis fixed-window counter |

### 第一次測試結果（global middleware）

| 指標 | Nginx (Ph9) | HAProxy + global MW |
|------|------------|---------------------|
| throughput | 2,661 req/s | **1,999 req/s（-25%）** |
| http_req_failed | 0% | 0.13% |
| seeded tokens | 500 | **60（rate limited!）** |

**兩個問題同時發生：**

**問題 A — Starlette `BaseHTTPMiddleware` 對所有請求加 overhead**

`@app.middleware("http")` 底層使用 `BaseHTTPMiddleware`，它將每個 response 包在 iterator 中，
並在 async 層多加一次 dispatch overhead。Redirect 路徑原本沒有任何 middleware，
加上後每個 request 都要多走一層，導致 -25% 退步。

**問題 B — Setup 階段被自己的 rate limit 擋住**

k6 setup 發送 500 次 create，全部從相同 IP 快速打出，
HAProxy 加上 `X-Forwarded-For: 127.0.0.1`，app 對同一 IP 計數超過 60/s → 440 次 429，
結果只有 60 個 token 被建立。

### 修正：global middleware → route dependency

```python
# routes.py — 只掛在 create route，不影響其他路徑
async def _rate_limit_create(request: Request) -> None:
    ip = request.headers.get("X-Forwarded-For", request.client.host).split(",")[0].strip()
    if not await cache.check_rate_limit(ip):
        raise HTTPException(status_code=429, detail="Too Many Requests")

@router.post("/api/qr/create", response_model=CreateResponse)
async def create_qr(
    req: CreateRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_rate_limit_create),
):
```

### 修正後 HAProxy 結果

| 指標 | Nginx (Ph9) | HAProxy + global MW | HAProxy + dependency |
|------|------------|---------------------|---------------------|
| throughput | **2,661 req/s** | 1,999 req/s | 2,520 req/s |
| http_req_failed | **0%** | 0.13% | 0.10% |

### 結論：假說被否定，回滾 Nginx

HAProxy 2,520 req/s 仍低於 Nginx 2,661 req/s（-5%），且有 0.10% 穩定失敗率。

- Nginx 對 HTTP reverse proxy 本身已高度優化，HAProxy 並無優勢
- 瓶頸從未在 LB 層，換 LB 無法突破 Python workers 上限
- 回滾成本低（nginx.conf 仍在 repo），且 rate limit 改善可獨立保留

---

## Stage 10c — Rate Limiting 正式遷移（保留）

從 Nginx `limit_req_zone` 遷移到 FastAPI route dependency，為本次唯一的正向改動。

### 架構比較

| 面向 | Nginx limit_req_zone | FastAPI dependency |
|------|---------------------|-------------------|
| 作用位置 | LB 層，Python 之前 | App 層，route handler 之前 |
| Redirect 路徑 overhead | 無 | **無**（dependency 只掛 create） |
| 維度擴展性 | 只能 per-IP | 可改為 per-user、per-token |
| Redis 依賴 | 不需要 | 需要（已有） |
| create 多一次 Redis | 無 | +0.5ms（在 42ms p50 下可忽略） |

### 實作細節

`cache.py` 新增 `check_rate_limit(ip, max_requests=60, window=1)`：

```python
async def check_rate_limit(ip: str, max_requests: int = 60, window: int = 1) -> bool:
    key = f"ratelimit:create:{ip}:{int(time.time()) // window}"
    count = await redis_client.incr(key)
    if count == 1:
        await redis_client.expire(key, window + 1)
    return count <= max_requests
```

- Fixed-window：每 1 秒一個 bucket，key 自動在 `window + 1` 秒後過期
- max_requests=60 等效於 Nginx 的 `rate=20r/s burst=40 nodelay`（20 + 40 = 60）
- Redis INCR 是原子操作，多 worker 下計數正確

### 驗證

```
80 個循序 create（同 IP）→ 60 個 200、20 個 429 ✓
```

---

## 學到的系統設計概念

### 1. Starlette BaseHTTPMiddleware 的效能陷阱

`@app.middleware("http")` 對 ALL 請求加 overhead，包括與 middleware 邏輯完全無關的路由。
在高吞吐量場景，任何 global middleware 都應評估其對熱路徑（redirect）的影響。

正確做法：**把 guard 邏輯放在 route dependency，只對目標 route 生效。**

### 2. 換 LB 無法突破 app worker 瓶頸

LB 層的 CPU 開銷是真實存在的，但規模遠小於 Python workers 的 CPU 上限。
除非 LB 本身成為 saturated bottleneck（通常需要 100k+ QPS），
否則換更輕量的 LB 無法突破 app 的吞吐量天花板。

### 3. 假說驗證優先，再動手改

本次實驗流程正確：先壓測確認假說（Nginx 是瓶頸），再決定是否實作。
直連壓測結果顯示 Nginx 不是主因後，HAProxy 實驗雖然仍進行，
但最終數字支持了「假說錯誤」的結論，並以低成本回滾。

---

## 最終狀態

架構與 Phase 9 終態相同，唯一差異：

| 項目 | Phase 9 | Phase 10 |
|------|---------|---------|
| Rate limit 位置 | Nginx `limit_req_zone` | FastAPI route dependency |
| Rate limit 維度 | Per-IP（Nginx 層） | Per-IP（Redis，可擴展） |
| LB | Nginx | Nginx（不變） |
| redirect throughput | 2,661 req/s | 2,661 req/s（不變） |

## Commits

- `24d4006` — `refactor(rate-limit): move rate limiting from middleware to route dependency`
