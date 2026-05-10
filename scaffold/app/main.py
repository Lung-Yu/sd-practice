import os
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from prometheus_fastapi_instrumentator import Instrumentator

from . import cache
from .database import Base, engine
from .routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    cache.redis_client = aioredis.from_url(
        os.getenv("REDIS_URL", "redis://localhost:6479/0"),
        decode_responses=False,
    )
    yield
    await cache.redis_client.aclose()


app = FastAPI(title="QR Code Generator", lifespan=lifespan)
app.include_router(router)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

Instrumentator().instrument(app).expose(app)
