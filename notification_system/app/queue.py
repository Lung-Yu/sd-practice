"""
Redis Stream helpers — shared by the HTTP API (producer) and the delivery worker (consumer).

Stream layout:
  notifications:delivery   STREAM  — pending delivery jobs
  Consumer group: delivery-workers
  Each worker uses socket.gethostname() as consumer name → unique per container.
"""
import redis as _redis

from . import config

STREAM_KEY = "notifications:delivery"
GROUP_NAME = "delivery-workers"

_client: _redis.Redis | None = None


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


def enqueue(notification_id: str) -> None:
    _get_client().xadd(STREAM_KEY, {"notification_id": notification_id})
