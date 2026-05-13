import random
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FuturesTimeout
from datetime import datetime

from . import config
from .channels.base import ChannelDeliveryError
from .channels.registry import get_channel
from .metrics import (
    delivery_timeouts,
    notification_delivery_seconds,
    notification_retries,
    notifications_sent,
)
from .models import Notification, NotificationStatus
from .store import store

# One shared pool per worker process; size >= MAX_RETRIES concurrent deliveries.
_executor = ThreadPoolExecutor(max_workers=32, thread_name_prefix="channel")


def _send_with_timeout(channel, user_id: str, message: str) -> None:
    future = _executor.submit(channel.send, user_id, message)
    try:
        future.result(timeout=config.ATTEMPT_TIMEOUT_S)
    except _FuturesTimeout:
        raise ChannelDeliveryError(f"timed out after {config.ATTEMPT_TIMEOUT_S}s")


def deliver(notification: Notification) -> Notification:
    channel = get_channel(notification.channel)
    last_error = ""

    with notification_delivery_seconds.labels(channel=notification.channel).time():
        for attempt in range(1, config.MAX_RETRIES + 1):
            notification.attempts = attempt
            if attempt > 1:
                notification_retries.labels(channel=notification.channel).inc()

            try:
                _send_with_timeout(channel, notification.user_id, notification.message)
                notification.status = NotificationStatus.SENT
                notification.sent_at = datetime.utcnow()
                store.save(notification)
                notifications_sent.labels(channel=notification.channel, status="SENT").inc()
                return notification

            except ChannelDeliveryError as e:
                last_error = str(e)
                if "timed out" in last_error:
                    delivery_timeouts.labels(channel=notification.channel).inc()
                if attempt < config.MAX_RETRIES:
                    delay = config.RETRY_BASE_DELAY_S * (2 ** (attempt - 1))
                    jitter = random.uniform(0, delay * 0.1)
                    time.sleep(delay + jitter)

    notification.status = NotificationStatus.FAILED
    notification.error = last_error
    store.save(notification)
    notifications_sent.labels(channel=notification.channel, status="FAILED").inc()
    return notification
