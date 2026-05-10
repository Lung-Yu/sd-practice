from datetime import datetime

import redis.asyncio as aioredis

redis_client: aioredis.Redis | None = None


async def get_cached_url(token: str) -> str | None:
    if redis_client is None:
        return None
    value = await redis_client.get(f"r:{token}")
    return value.decode() if value else None


async def set_cached_url(token: str, url: str) -> None:
    if redis_client:
        await redis_client.set(f"r:{token}", url)


async def delete_cached_url(token: str) -> None:
    if redis_client:
        await redis_client.delete(f"r:{token}")


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
