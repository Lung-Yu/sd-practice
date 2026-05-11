from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import cache
from .database import get_db
from .metrics import cache_hits, cache_misses, qr_created, redirects
from .models import ScanEvent, UrlMapping
from .schemas import CreateRequest, CreateResponse, QRInfoResponse, UpdateRequest
from .token_gen import generate_token
from .url_validator import validate_url

router = APIRouter()

BASE_URL = "http://localhost:8100"


@router.post("/api/qr/create", response_model=CreateResponse)
async def create_qr(req: CreateRequest, db: AsyncSession = Depends(get_db)):
    try:
        normalized_url = validate_url(req.url)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    for _ in range(10):
        token = generate_token(normalized_url)
        mapping = UrlMapping(
            token=token,
            original_url=normalized_url,
            expires_at=req.expires_at,
        )
        db.add(mapping)
        try:
            await db.commit()
            break
        except IntegrityError:
            await db.rollback()
    else:
        raise HTTPException(status_code=500, detail="Token generation failed")

    short_url = f"{BASE_URL}/r/{token}"
    ttl = None
    if mapping.expires_at:
        ttl = int((mapping.expires_at - datetime.utcnow()).total_seconds())
    await cache.set_cached_url(token, normalized_url, ttl=ttl)
    qr_created.inc()

    return CreateResponse(
        token=token,
        short_url=short_url,
        original_url=normalized_url,
    )


@router.get("/r/{token}")
async def redirect(token: str, request: Request, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    """Cache-first redirect: Redis -> negative cache -> DB -> 404/410."""
    cached_url = await cache.get_cached_url(token)
    if cached_url:
        cache_hits.inc()
        background_tasks.add_task(_enqueue_scan, token, request)
        redirects.labels(status="302").inc()
        return RedirectResponse(url=cached_url, status_code=302)

    if await cache.is_cached_gone(token):
        redirects.labels(status="404").inc()
        raise HTTPException(status_code=404, detail="Not Found")

    cache_misses.inc()
    result = await db.execute(select(UrlMapping).filter(UrlMapping.token == token))
    mapping = result.scalar_one_or_none()
    if mapping is None:
        await cache.set_cached_gone(token)
        redirects.labels(status="404").inc()
        raise HTTPException(status_code=404, detail="Not Found")
    if mapping.is_deleted or (mapping.expires_at and mapping.expires_at < datetime.utcnow()):
        await cache.set_cached_gone(token)
        redirects.labels(status="410").inc()
        raise HTTPException(status_code=410, detail="Gone")

    ttl = None
    if mapping.expires_at:
        ttl = int((mapping.expires_at - datetime.utcnow()).total_seconds())
    await cache.set_cached_url(token, mapping.original_url, ttl=ttl)
    background_tasks.add_task(_enqueue_scan, token, request)
    redirects.labels(status="302").inc()
    return RedirectResponse(url=mapping.original_url, status_code=302)


@router.get("/api/qr/{token}", response_model=QRInfoResponse)
async def get_qr_info(token: str, db: AsyncSession = Depends(get_db)):
    return await _get_mapping_or_404(token, db)


@router.patch("/api/qr/{token}", response_model=QRInfoResponse)
async def update_qr(token: str, req: UpdateRequest, db: AsyncSession = Depends(get_db)):
    mapping = await _get_mapping_or_404(token, db)

    if req.url is not None:
        try:
            mapping.original_url = validate_url(req.url)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        await cache.delete_cached_url(token)

    if req.expires_at is not None:
        mapping.expires_at = req.expires_at
        await cache.delete_cached_url(token)

    await db.commit()
    await db.refresh(mapping)
    return mapping


@router.delete("/api/qr/{token}")
async def delete_qr(token: str, db: AsyncSession = Depends(get_db)):
    mapping = await _get_mapping_or_404(token, db)
    mapping.is_deleted = True
    await db.commit()
    await cache.delete_cached_url(token)
    return {"detail": "Deleted"}


@router.get("/api/qr/{token}/analytics")
async def get_analytics(token: str, db: AsyncSession = Depends(get_db)):
    await _get_mapping_or_404(token, db)

    result = await db.execute(
        select(func.count(ScanEvent.id)).filter(ScanEvent.token == token)
    )
    total = result.scalar()

    result = await db.execute(
        select(
            func.date(ScanEvent.scanned_at).label("date"),
            func.count(ScanEvent.id).label("count"),
        )
        .filter(ScanEvent.token == token)
        .group_by(func.date(ScanEvent.scanned_at))
    )
    daily = result.all()

    return {
        "token": token,
        "total_scans": total,
        "scans_by_day": [{"date": str(row.date), "count": row.count} for row in daily],
    }


async def _get_mapping_or_404(token: str, db: AsyncSession) -> UrlMapping:
    result = await db.execute(select(UrlMapping).filter(UrlMapping.token == token))
    mapping = result.scalar_one_or_none()
    if mapping is None or mapping.is_deleted:
        raise HTTPException(status_code=404, detail="Not Found")
    return mapping


async def _enqueue_scan(token: str, request: Request) -> None:
    await cache.enqueue_scan(
        token=token,
        user_agent=request.headers.get("user-agent", ""),
        ip=request.client.host if request.client else "",
    )
