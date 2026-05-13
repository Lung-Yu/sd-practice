"""
Simple consecutive-failure circuit breaker.

States:
  CLOSED    → normal; pass all calls through
  OPEN      → fail-fast; raise CircuitOpenError without calling the target
  HALF_OPEN → one probe allowed; close on success, reopen on failure

One breaker instance per channel (managed in channels/registry.py).
Thread-safe via a single Lock.
"""
import threading
import time
from enum import Enum


class _State(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised when a call is rejected because the circuit is OPEN."""


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_seconds: float = 30.0,
    ) -> None:
        self.name = name
        self._threshold = failure_threshold
        self._recovery_s = recovery_seconds
        self._state = _State.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        return self._state.value

    def call(self, fn, *args, **kwargs):
        with self._lock:
            if self._state == _State.OPEN:
                elapsed = time.monotonic() - (self._opened_at or 0)
                if elapsed >= self._recovery_s:
                    self._state = _State.HALF_OPEN
                else:
                    raise CircuitOpenError(
                        f"circuit {self.name!r} is OPEN — retry in "
                        f"{self._recovery_s - elapsed:.0f}s"
                    )

        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            self._on_failure()
            raise
        else:
            self._on_success()
            return result

    def _on_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._state = _State.CLOSED
            self._opened_at = None

    def _on_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if (
                self._state == _State.HALF_OPEN
                or self._consecutive_failures >= self._threshold
            ):
                self._state = _State.OPEN
                self._opened_at = time.monotonic()
                self._consecutive_failures = 0
