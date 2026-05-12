import uuid

from fastapi import APIRouter, HTTPException

from .delivery import deliver
from .idempotency import compute_key
from .models import Notification
from .schemas import NotificationDetail, NotificationSummary, SendRequest, SendResponse
from .store import store

router = APIRouter()


@router.post("/send", response_model=SendResponse)
def send_notification(req: SendRequest) -> SendResponse:
    try:
        from .channels.registry import get_channel
        get_channel(req.channel)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    key = compute_key(req.user_id, req.topic, req.message)
    existing = store.get_by_key(key)
    if existing is not None:
        return SendResponse(notification_id=existing.notification_id, status=existing.status)

    notification = Notification(
        notification_id=str(uuid.uuid4()),
        user_id=req.user_id,
        channel=req.channel,
        message=req.message,
        topic=req.topic,
        idempotency_key=key,
    )
    store.save(notification)
    deliver(notification)
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
