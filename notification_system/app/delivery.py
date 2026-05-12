from datetime import datetime

from . import config
from .channels.base import ChannelDeliveryError
from .channels.registry import get_channel
from .models import Notification, NotificationStatus
from .store import store


def deliver(notification: Notification) -> Notification:
    channel = get_channel(notification.channel)
    last_error = ""

    for attempt in range(1, config.MAX_RETRIES + 1):
        notification.attempts = attempt
        try:
            channel.send(notification.user_id, notification.message)
            notification.status = NotificationStatus.SENT
            notification.sent_at = datetime.utcnow()
            store.save(notification)
            return notification
        except ChannelDeliveryError as e:
            last_error = str(e)

    notification.status = NotificationStatus.FAILED
    notification.error = last_error
    store.save(notification)
    return notification
