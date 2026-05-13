from prometheus_client import Counter, Histogram

notifications_sent = Counter(
    "notifications_sent_total",
    "Notifications by delivery outcome",
    ["channel", "status"],
)

notification_delivery_seconds = Histogram(
    "notification_delivery_duration_seconds",
    "End-to-end delivery time per channel (all attempts combined)",
    ["channel"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)

notification_retries = Counter(
    "notification_retries_total",
    "Retry attempts after first failure, by channel",
    ["channel"],
)

delivery_timeouts = Counter(
    "delivery_timeouts_total",
    "Delivery attempts that timed out, by channel",
    ["channel"],
)

idempotency_hits = Counter(
    "idempotency_hits_total",
    "Requests deduplicated by idempotency key (no re-delivery)",
)
