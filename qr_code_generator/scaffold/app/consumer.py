import asyncio
import os
from datetime import datetime

from .database import AsyncSessionLocal
from .models import ScanEvent

_GROUP = "scan_workers"


async def scan_consumer() -> None:
    from . import cache

    consumer_name = f"worker-{os.getpid()}"

    while cache.redis_client is None:
        await asyncio.sleep(1)

    # Create consumer group if it doesn't exist yet (races between workers are safe — BUSYGROUP is swallowed)
    try:
        await cache.redis_client.xgroup_create("scan_events", _GROUP, id="0", mkstream=True)
    except Exception:
        pass

    while True:
        if cache.redis_client is None:
            await asyncio.sleep(1)
            continue
        try:
            events = await cache.redis_client.xreadgroup(
                _GROUP,
                consumer_name,
                {"scan_events": ">"},
                count=200,
                block=500,
            )
        except Exception:
            await asyncio.sleep(1)
            continue

        if not events:
            continue

        _, messages = events[0]
        if not messages:
            continue

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

        msg_ids = [msg_id for msg_id, _ in messages]
        await cache.redis_client.xack("scan_events", _GROUP, *msg_ids)
