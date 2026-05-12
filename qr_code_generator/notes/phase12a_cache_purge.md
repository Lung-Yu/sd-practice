# Phase 12a：Varnish 快取 PURGE 實作筆記

## 功能說明

**功能：QR Code 更新／刪除時自動清除 Varnish 快取**

當 QR Code 的 URL 或到期時間被更新（`PATCH /api/qr/{token}`）或刪除（`DELETE /api/qr/{token}`）時，應用程式會自動發送 HTTP PURGE 請求給 Varnish，立即使快取的 302 重導向失效，避免在 60 秒 TTL 窗口內返回過期的回應。

---

## 變更檔案

| 檔案 | 變更內容 |
|------|--------|
| `varnish/default.vcl` | 新增 PURGE 處理器（`X-Purge-Token` 驗證）與 `vcl_hash` 覆寫（僅依 URL 雜湊） |
| `scaffold/requirements.txt` | 新增 `httpx[asyncio]==0.27.2` |
| `scaffold/app/cache.py` | 新增 `async def purge_varnish_cache(token: str) -> bool` |
| `scaffold/app/routes.py` | 在 `update_qr` 與 `delete_qr` 中新增 `await cache.purge_varnish_cache(token)` |

---

## 測試過程發現的關鍵 Bug 與修正

### Bug：VCL 雜湊不一致導致 PURGE 無效

初始的 PURGE 實作無效，原因在於 Varnish 預設的 `vcl_hash` 會同時使用 URL 與 Host 標頭進行雜湊。外部客戶端透過 `Host: localhost:8200` 存取 Varnish，但應用程式內部的 PURGE 請求使用 `Host: varnish:80`。雜湊值不同，導致 PURGE 無法命中已快取的物件。

### 修正方式

在 VCL 中新增 `sub vcl_hash`，只對 `req.url` 進行雜湊，忽略 Host 標頭。如此一來，不論是內部或外部的請求，都能命中同一筆快取記錄。

---

## 測試結果

測試流程：建立 token → 第一次 GET（MISS）→ 第二次 GET（HIT）→ PATCH URL → 第三次 GET（**MISS** = PURGE 生效）→ 第四次 GET（HIT，返回新 URL）

結果：✓ 自動 PURGE 全流程正常。執行 PATCH 後，Varnish 對第一次請求立即顯示 MISS，接著快取新的 302 重導向。

---

## Commits

- `8844beb` — `feat(purge): add Varnish cache PURGE on QR update/delete`
- `182d7c9` — `fix(purge): add vcl_hash to normalize cache key by URL only`

---

## 學習重點

### 1. Varnish PURGE 需要匹配雜湊鍵

Varnish 預設以 URL + Host 進行雜湊。內部 PURGE 請求必須：
- 方案 (a)：設定與外部客戶端相同的 Host 標頭，或
- 方案 (b)：設定 `vcl_hash` 改為只使用 URL。

方案 (b) 在正式環境中更為穩健，因為不依賴 Host 標頭的一致性。

### 2. PURGE 的安全性

絕對不能將 PURGE 端點對外公開。使用 `X-Purge-Token` 標頭進行驗證，對內部網路來說簡單且足夠安全。

### 3. 快取一致性的取捨

TTL = 60 秒，代表在沒有 PURGE 的情況下，快取資料最多可能落後 60 秒。加入 PURGE 後，一致性提升至近即時（受限於 PURGE HTTP 呼叫的延遲，約 1–5 ms）。

### 4. Fire-and-forget 模式在此適用

`purge_varnish_cache` 會回傳 `True`／`False`，但 route 層不檢查此回傳值。若 Varnish 停機，TTL 自然到期後快取會自動失效。這是正確的設計：PURGE 是一種盡力而為（best-effort）的優化，而非正確性的必要條件——應用程式仍可透過原始路徑返回正確資料。
