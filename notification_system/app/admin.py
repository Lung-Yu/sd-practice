"""
Admin endpoints — operational visibility and control.

  GET  /admin/health/channels   — circuit breaker state per channel
  GET  /admin/dlq               — DLQ depth + up to 10 sample IDs
  POST /admin/dlq/retry         — re-enqueue up to N DLQ entries for delivery
"""
from fastapi import APIRouter, HTTPException

from . import config
from .channels.registry import get_breaker_states

admin_router = APIRouter(prefix="/admin", tags=["admin"])


@admin_router.get("/health/channels")
def channel_health() -> dict:
    """Current circuit breaker state for each channel (closed / open / half_open)."""
    return {"circuit_breakers": get_breaker_states()}


@admin_router.get("/dlq")
def dlq_status() -> dict:
    if not config.REDIS_URL:
        raise HTTPException(status_code=503, detail="Redis not configured")
    from .queue import DLQ_KEY, _get_client, dlq_length
    r = _get_client()
    length = dlq_length()
    sample = r.lrange(DLQ_KEY, 0, 9)  # peek at first 10 without consuming
    return {"depth": length, "sample": sample}


@admin_router.post("/dlq/retry")
def dlq_retry(count: int = 100) -> dict:
    """Pop up to *count* entries from the DLQ and re-enqueue for delivery."""
    if not config.REDIS_URL:
        raise HTTPException(status_code=503, detail="Redis not configured")
    from .queue import dlq_retry_batch
    requeued = dlq_retry_batch(count)
    return {"requeued": len(requeued), "notification_ids": requeued}
