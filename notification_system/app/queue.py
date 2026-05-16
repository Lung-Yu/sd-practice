"""
Redis Stream helpers — shared by the HTTP API (producer) and the delivery worker (consumer).

Stream layout:
  notifications:delivery   STREAM  — pending delivery jobs
  Consumer group: delivery-workers
  Each worker uses socket.gethostname() as consumer name → unique per container.
"""
import redis as _redis
import redis.asyncio as _aioredis

from . import config

STREAM_KEY = "notifications:delivery"
GROUP_NAME = "delivery-workers"

_client: _redis.Redis | None = None
_async_client: _aioredis.Redis | None = None


def _get_client() -> _redis.Redis:
    global _client
    if _client is None:
        _client = _redis.from_url(config.REDIS_URL, decode_responses=True)
    return _client


def ensure_group() -> None:
    """Create the consumer group if it doesn't exist. Safe to call multiple times."""
    try:
        _get_client().xgroup_create(STREAM_KEY, GROUP_NAME, id="0", mkstream=True)
    except _redis.exceptions.ResponseError:
        pass  # already exists


def _get_async_client() -> _aioredis.Redis:
    global _async_client
    if _async_client is None:
        _async_client = _aioredis.from_url(config.REDIS_URL, decode_responses=True, max_connections=1000)
    return _async_client


def enqueue(notification_id: str) -> None:
    _get_client().xadd(STREAM_KEY, {"notification_id": notification_id})


async def aenqueue(notification_id: str) -> None:
    await _get_async_client().xadd(STREAM_KEY, {"notification_id": notification_id})


# ---------------------------------------------------------------------------
# Dead-Letter Queue — notifications that exhausted all retries
# ---------------------------------------------------------------------------

DLQ_KEY = "notifications:dlq"


def enqueue_dlq(notification_id: str) -> None:
    _get_client().rpush(DLQ_KEY, notification_id)


def dlq_length() -> int:
    return int(_get_client().llen(DLQ_KEY))


def dlq_retry_batch(count: int = 100) -> list[str]:
    """Pop up to *count* IDs from the DLQ and re-enqueue them for delivery.
    Returns the list of re-queued IDs."""
    r = _get_client()
    ids: list[str] = []
    pipe = r.pipeline()
    for _ in range(count):
        pipe.lpop(DLQ_KEY)
    results = pipe.execute()
    for nid in results:
        if nid:
            r.xadd(STREAM_KEY, {"notification_id": nid})
            ids.append(nid)
    return ids
