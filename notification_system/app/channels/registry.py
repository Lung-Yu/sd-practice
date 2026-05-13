from .. import config
from ..circuit_breaker import CircuitBreaker, CircuitOpenError
from ..metrics import circuit_breaker_trips
from .base import BaseChannel, ChannelDeliveryError
from .email import EmailChannel
from .push import PushChannel
from .sms import SMSChannel

_REGISTRY: dict[str, type[BaseChannel]] = {
    "email": EmailChannel,
    "sms": SMSChannel,
    "push": PushChannel,
}

# One breaker per channel name — module-level so state persists across requests.
_BREAKERS: dict[str, CircuitBreaker] = {}


class _ProtectedChannel(BaseChannel):
    """Wraps a channel's send() with circuit-breaker protection."""

    def __init__(self, inner: BaseChannel, breaker: CircuitBreaker) -> None:
        self._inner = inner
        self._breaker = breaker

    def send(self, user_id: str, message: str) -> None:
        try:
            self._breaker.call(self._inner.send, user_id, message)
        except CircuitOpenError as exc:
            circuit_breaker_trips.labels(channel=self._breaker.name).inc()
            raise ChannelDeliveryError(str(exc))


def get_channel(name: str) -> BaseChannel:
    cls = _REGISTRY.get(name.lower())
    if cls is None:
        raise ValueError(f"Unknown channel: {name!r}. Valid channels: {list(_REGISTRY)}")

    breaker = _BREAKERS.setdefault(
        name,
        CircuitBreaker(
            name,
            failure_threshold=config.CB_FAILURE_THRESHOLD,
            recovery_seconds=config.CB_RECOVERY_SECONDS,
        ),
    )
    return _ProtectedChannel(cls(), breaker)


def get_breaker_states() -> dict[str, str]:
    return {name: b.state for name, b in _BREAKERS.items()}
