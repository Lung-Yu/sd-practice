from prometheus_client import Counter

qr_created = Counter("qr_codes_created_total", "QR codes created")
redirects = Counter("qr_redirects_total", "Redirects by outcome", ["status"])
cache_hits = Counter("qr_cache_hits_total", "Redirect cache hits")
cache_misses = Counter("qr_cache_misses_total", "Redirect cache misses")
