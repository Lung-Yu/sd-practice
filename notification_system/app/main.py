import time

import redis as _redis
from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from . import config
from .admin import admin_router
from .routes import router

app = FastAPI(title="Notification System")
app.include_router(router, prefix="/api/notifications")
app.include_router(admin_router)


@app.on_event("startup")
def _wait_for_redis() -> None:
    """Block startup until Redis is ready (handles BusyLoadingError on AOF replay)."""
    if not config.REDIS_URL:
        return
    r = _redis.from_url(config.REDIS_URL, socket_connect_timeout=2)
    for attempt in range(1, 31):
        try:
            r.ping()
            return
        except (_redis.exceptions.BusyLoadingError, _redis.exceptions.ConnectionError):
            time.sleep(1.0)


@app.get("/")
def health() -> dict:
    return {"status": "ok"}


@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
