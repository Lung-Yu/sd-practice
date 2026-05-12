from fastapi import FastAPI

from .routes import router

app = FastAPI(title="Notification System")
app.include_router(router, prefix="/api/notifications")


@app.get("/")
def health() -> dict:
    return {"status": "ok"}
