import asyncio
from datetime import datetime

from .database import AsyncSessionLocal
from .models import ScanEvent


async def scan_consumer() -> None:
    from . import cache  # late import avoids circular dependency at module load
    last_id = "0"
    while True:
        if cache.redis_client is None:
            await asyncio.sleep(1)
            continue
        try:
            events = await cache.redis_client.xread(
                {"scan_events": last_id}, count=200, block=500
            )
        except Exception:
            await asyncio.sleep(1)
            continue
        if not events:
            continue
        _, messages = events[0]
        async with AsyncSessionLocal() as db:
            db.add_all([
                ScanEvent(
                    token=m[b"token"].decode(),
                    user_agent=m[b"user_agent"].decode() or None,
                    ip_address=m[b"ip"].decode() or None,
                    scanned_at=datetime.fromisoformat(m[b"ts"].decode()),
                )
                for _, m in messages
            ])
            await db.commit()
        last_id = messages[-1][0]
