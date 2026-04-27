"""Server-protection semaphores — journeys, uploads, rebuilds.

Each gate enforces an in-flight limit read from `platform_config`. Excess
requests are **rejected** (not queued) so the server stays responsive under
demo load. Rejections are themselves logged by the call site (see spec §11.3,
§12.3).

Hot-swap: `semaphores.reload_from_config(...)` updates the limits atomically;
in-flight requests are unaffected; new arrivals see the new limit.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator


log = logging.getLogger(__name__)


class ConcurrencyExceeded(Exception):
    """Raised when a gate is full. The caller maps this to HTTP 503."""

    def __init__(self, gate: str, limit: int) -> None:
        super().__init__(f"{gate} concurrency limit reached ({limit})")
        self.gate = gate
        self.limit = limit


class ConcurrencyGate:
    """Non-blocking, configurable concurrency gate.

    `acquire_or_fail()` is an async context manager that either admits the
    request immediately or raises `ConcurrencyExceeded`. We deliberately do
    not queue: queue depth itself becomes a failure mode under load.
    """

    def __init__(self, name: str, limit: int) -> None:
        self.name = name
        self._limit = limit
        self._lock = asyncio.Lock()
        self._in_flight = 0

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def set_limit(self, new_limit: int) -> None:
        """Hot-swap the limit. Existing in-flight requests are not interrupted."""
        if new_limit < 1:
            raise ValueError(f"limit must be >= 1, got {new_limit}")
        if self._limit != new_limit:
            log.info("gate %s: limit %d → %d", self.name, self._limit, new_limit)
        self._limit = new_limit

    @asynccontextmanager
    async def acquire_or_fail(self) -> AsyncIterator[None]:
        async with self._lock:
            if self._in_flight >= self._limit:
                raise ConcurrencyExceeded(self.name, self._limit)
            self._in_flight += 1
        try:
            yield
        finally:
            async with self._lock:
                self._in_flight -= 1


class Semaphores:
    """Singleton bag of gates. Initialised once; reloaded on config PATCH."""

    def __init__(self) -> None:
        # Bootstrap with safe defaults; replaced on first reload_from_config().
        self.journey = ConcurrencyGate("journey", 20)
        self.upload = ConcurrencyGate("upload", 3)
        self.rebuild = ConcurrencyGate("rebuild", 1)
        self._initialised = False

    def reload_from_config(self, cfg: dict[str, Any]) -> None:
        self.journey.set_limit(int(cfg["MAX_CONCURRENT_JOURNEYS"]))
        self.upload.set_limit(int(cfg["MAX_CONCURRENT_UPLOADS"]))
        self.rebuild.set_limit(int(cfg["MAX_CONCURRENT_REBUILDS"]))
        self._initialised = True

    @property
    def initialised(self) -> bool:
        return self._initialised


semaphores = Semaphores()
