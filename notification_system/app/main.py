from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .admin import admin_router
from .routes import router

app = FastAPI(title="Notification System")
app.include_router(router, prefix="/api/notifications")
app.include_router(admin_router)


@app.get("/")
def health() -> dict:
    return {"status": "ok"}


@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
