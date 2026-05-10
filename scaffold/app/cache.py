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
