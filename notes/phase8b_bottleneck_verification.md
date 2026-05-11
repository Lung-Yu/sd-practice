# Phase 8b — Nginx/CPU 瓶頸驗證（2026-05-11）

## 問題

Phase 8 發現 redirect 上限 ~1,731 req/s，且加 worker 反而更差。
推論：Nginx 或 Podman VM CPU 是瓶頸。本次驗證到底是哪個。

## 驗證方法

1. 暫時給 app1 加 port mapping（8001:8000），繞過 Nginx 直打
2. 比較有無 Nginx 的 throughput
3. 把 Podman VM 從 5 vCPU 升到 8 vCPU，再測

## 實驗結果

| 測試條件 | vCPU | workers/container | 途徑 | Throughput | p50 | p95 |
|---------|------|------------------|------|-----------|-----|-----|
| 5 vCPU, 4w | 5 | 4 | Nginx | 1,731 req/s | 687ms | 5.59s |
| 5 vCPU, 4w | 5 | 4 | 直打 app1 | 2,116 req/s | 1.26s | 2.28s |
| 8 vCPU, 4w | 8 | 4 | Nginx | 2,530 req/s | 47ms | 3.29s |
| 8 vCPU, 6w | 8 | 6 | Nginx | **2,605 req/s** | **36ms** | **2.73s** |
| 8 vCPU, 8w | 8 | 8 | Nginx | 2,649 req/s | 48ms | 2.64s |

## 結論

### 1. Nginx 有真實開銷，但不是主要瓶頸

- 直打 app1（無 Nginx）：2,116 req/s
- via Nginx（5 vCPU）：1,731 req/s → Nginx 額外損耗 ~385 req/s（18%）
- 但 Nginx 本身設計沒問題（5 個 worker，完全對應 5 vCPU auto）

### 2. CPU contention 是真正根因

- 5 vCPU 時：8 Nginx workers + 8 Python workers + PG + PgBouncer + Redis + Prometheus + Grafana = 18+ 重型 process 搶 5 vCPU
- 5→8 vCPU（+60% CPU）：throughput 1,731→2,605（+50%），接近線性
- p50 從 687ms → 36ms：系統不再飽和，請求不需大量排隊

### 3. Worker 數的最優解取決於 CPU 預算

| 環境 | 最優 workers/container | 原因 |
|------|----------------------|------|
| 5 vCPU Podman | 4 | 超過就競爭 CPU |
| 8 vCPU Podman | 6 | 甜蜜點（p50 最低，throughput 接近 8w） |
| 生產（獨立 VM） | vCPU 數 | app/Nginx 不競爭，可線性擴展 |

**Sweet spot 計算邏輯：**
```
8w 比 6w throughput +1.7%，但 p50 變差（48ms vs 36ms）
→ 16 個 Python process + 8 個 Nginx process = 24 個 heavy process on 8 vCPU
→ context switch overhead 開始超過多 worker 的收益
```

### 4. 到 5,000 QPS 需要多少資源（本地 Podman 估算）

```
每 vCPU ≈ 325 redirect req/s（8 vCPU / 2,605 req/s）
5,000 req/s ÷ 325 ≈ 15-16 vCPU

或者：2,605 × (N/8) ≈ 5,000 → N ≈ 15 vCPU
```

在生產環境（Nginx + app 各自獨立機器）：
- Nginx 不消耗 app 的 CPU → app 單機 throughput 更高
- 可以更容易達到 5,000 QPS

## 系統設計概念

### CPU Over-subscription 的非線性代價
- 少量 over-subscription（1.2x）：幾乎無感
- 中等（2x）：context switch 開銷明顯，throughput 下降
- 嚴重（3x+）：p50/p95 大幅惡化，系統看起來「卡住」

本例：5 vCPU 上 18+ 重型 process = 3.6x over-subscription → 687ms p50
8 vCPU 上 24 process = 3x over-subscription → 36ms p50

### 為什麼生產環境通常 workers = 2 × vCPU?
- 對 CPU-bound 工作：workers = vCPU（Python GIL，每 worker 一個 GIL holder）
- 對 I/O-bound 工作（如 async uvicorn）：workers = 1~2 × vCPU，超過無益因為 GIL 不是瓶頸，是事件循環 CPU 開銷
- 本例：6w on 8 vCPU = 12 Python workers + 8 Nginx = 20/8 = 2.5x，稍微 over-subscribed 但可接受

### Nginx auto worker 的含義
- `worker_processes auto` 在 Linux 讀取 `/proc/cpuinfo`
- 在 Podman VM：5 vCPU → 5 workers；8 vCPU → 8 workers
- Nginx worker 是真正的多進程（不是 thread），完全利用多核
- Nginx 的 CPU 消耗很低（mostly kernel networking），但每個 worker 確實佔一個 OS thread slot

## commit

`7cec6f0` — 推送至 https://github.com/Lung-Yu/sd-practice-qr-code-generator
