import time
from datetime import datetime

import redis.asyncio as aioredis

redis_client: aioredis.Redis | None = None

_DEFAULT_TTL = 86400  # 24h fallback when no expires_at


async def get_cached_url(token: str) -> str | None:
    if redis_client is None:
        return None
    value = await redis_client.get(f"r:{token}")
    return value.decode() if value else None


async def set_cached_url(token: str, url: str, ttl: int | None = None) -> None:
    if redis_client:
        ex = ttl if ttl and ttl > 0 else _DEFAULT_TTL
        await redis_client.set(f"r:{token}", url, ex=ex)


async def delete_cached_url(token: str) -> None:
    if redis_client:
        await redis_client.delete(f"r:{token}")
        await redis_client.delete(f"gone:{token}")


async def is_cached_gone(token: str) -> bool:
    if redis_client is None:
        return False
    return await redis_client.exists(f"gone:{token}") > 0


async def set_cached_gone(token: str) -> None:
    if redis_client:
        await redis_client.set(f"gone:{token}", b"1", ex=60)


async def check_rate_limit(ip: str, max_requests: int = 60, window: int = 1) -> bool:
    """Returns True if request is allowed. Fixed-window: max_requests per window seconds per IP."""
    if redis_client is None:
        return True
    key = f"ratelimit:create:{ip}:{int(time.time()) // window}"
    count = await redis_client.incr(key)
    if count == 1:
        await redis_client.expire(key, window + 1)
    return count <= max_requests


async def enqueue_scan(token: str, user_agent: str, ip: str) -> None:
    if redis_client:
        await redis_client.xadd(
            "scan_events",
            {
                b"token": token.encode(),
                b"user_agent": user_agent.encode(),
                b"ip": ip.encode(),
                b"ts": datetime.utcnow().isoformat().encode(),
            },
            maxlen=100000,
        )
