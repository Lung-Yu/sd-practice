from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from prometheus_fastapi_instrumentator import Instrumentator

from .database import Base, engine
from .routes import router

Base.metadata.create_all(bind=engine)

app = FastAPI(title="QR Code Generator")
app.include_router(router)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

Instrumentator().instrument(app).expose(app)
