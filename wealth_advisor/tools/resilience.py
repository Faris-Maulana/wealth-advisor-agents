"""Retry and circuit-breaker primitives.

Both take injectable `sleep` / `clock` callables so tests (and the in-browser
demo) run deterministically without real waiting.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    base_delay_s: float = 0.2
    max_delay_s: float = 2.0
    jitter: float = 0.1
    sleep: Callable[[float], None] = field(default=time.sleep, repr=False)
    rng: random.Random = field(default_factory=lambda: random.Random(7), repr=False)

    def backoff_delays(self) -> Iterator[float]:
        """Delays to wait *between* attempts (len = max_attempts - 1)."""
        for attempt in range(1, self.max_attempts):
            delay = min(self.max_delay_s, self.base_delay_s * (2 ** (attempt - 1)))
            yield max(0.0, delay + self.rng.uniform(-self.jitter, self.jitter) * delay)


NO_RETRY = RetryPolicy(max_attempts=1)


class CircuitBreaker:
    """Classic closed -> open -> half-open breaker, one instance per tool.

    Prevents hammering an upstream that is clearly down: after
    `failure_threshold` consecutive retryable failures the breaker opens and
    calls fail immediately with `CircuitOpenError` until `reset_timeout_s`
    passes, after which a single trial call is allowed.
    """

    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

    def __init__(self, failure_threshold: int = 3, reset_timeout_s: float = 30.0,
                 clock: Callable[[], float] = time.monotonic):
        self.failure_threshold = failure_threshold
        self.reset_timeout_s = reset_timeout_s
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._state = self.CLOSED

    @property
    def state(self) -> str:
        if self._state == self.OPEN and self._opened_at is not None:
            if self._clock() - self._opened_at >= self.reset_timeout_s:
                self._state = self.HALF_OPEN
        return self._state

    def allow(self) -> bool:
        return self.state != self.OPEN

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._state = self.CLOSED

    def record_failure(self) -> None:
        self._failures += 1
        if self.state == self.HALF_OPEN or self._failures >= self.failure_threshold:
            self._state = self.OPEN
            self._opened_at = self._clock()

    def reset(self) -> None:
        self.record_success()
