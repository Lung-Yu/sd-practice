import time
import uuid

from fastapi import APIRouter, HTTPException

from . import config
from .delivery import deliver
from .idempotency import compute_key
from .metrics import idempotency_hits, rate_limit_hits
from .models import Notification
from .schemas import NotificationDetail, NotificationSummary, SendRequest, SendResponse
from .store import store

router = APIRouter()


def _check_rate_limit(user_id: str) -> bool:
    """Return True if the request is allowed; False if the user is over limit.

    Uses a fixed-window counter in Redis:
      key = ratelimit:{user_id}:{bucket}  (bucket = epoch // window_s)
    INCR + conditional EXPIRE — two round-trips but no Lua needed.
    """
    if not config.REDIS_URL:
        return True
    from .queue import _get_client
    r = _get_client()
    bucket = int(time.time()) // config.RATE_LIMIT_WINDOW_S
    key = f"ratelimit:{user_id}:{bucket}"
    count = r.incr(key)
    if count == 1:
        r.expire(key, config.RATE_LIMIT_WINDOW_S)
    return count <= config.RATE_LIMIT_PER_USER


@router.post("/send", status_code=202, response_model=SendResponse)
def send_notification(req: SendRequest) -> SendResponse:
    if not _check_rate_limit(req.user_id):
        rate_limit_hits.inc()
        raise HTTPException(status_code=429, detail="rate limit exceeded — try again later")

    try:
        from .channels.registry import get_channel
        get_channel(req.channel)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    key = compute_key(req.user_id, req.topic, req.message)
    existing = store.get_by_key(key)
    if existing is not None:
        idempotency_hits.inc()
        return SendResponse(notification_id=existing.notification_id, status=existing.status)

    notification = Notification(
        notification_id=str(uuid.uuid4()),
        user_id=req.user_id,
        channel=req.channel,
        message=req.message,
        topic=req.topic,
        idempotency_key=key,
    )
    store.save(notification)  # persist as PENDING — visible to GET immediately

    if config.REDIS_URL:
        from .queue import enqueue
        enqueue(notification.notification_id)  # worker picks this up and delivers
    else:
        deliver(notification)  # in-memory fallback: synchronous delivery

    return SendResponse(notification_id=notification.notification_id, status=notification.status)


@router.get("/", response_model=list[NotificationSummary])
def list_notifications(user_id: str) -> list[NotificationSummary]:
    notifications = store.list_for_user(user_id)
    return [NotificationSummary(notification_id=n.notification_id, status=n.status) for n in notifications]


@router.get("/{notification_id}", response_model=NotificationDetail)
def get_notification(notification_id: str) -> NotificationDetail:
    notification = store.get(notification_id)
    if notification is None:
        raise HTTPException(status_code=404, detail=f"Notification {notification_id!r} not found")
    return NotificationDetail(
        notification_id=notification.notification_id,
        user_id=notification.user_id,
        channel=notification.channel,
        status=notification.status,
        created_at=notification.created_at,
        sent_at=notification.sent_at,
        error=notification.error,
    )
