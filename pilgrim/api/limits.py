"""Per-client rate limits for the public routes (RADIO §11).
"""
from __future__ import annotations

import logging
import math
import time
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    from pilgrim.server import PublicSurface

log = logging.getLogger("radio.server")


def _make_rate_limiter(station: PublicSurface,
                       limit: int | None = None) -> Callable[[Request], bool]:
    """Per-app request-line rate limit: client IP -> timestamps (4.3).

    Keyed on the resolved client IP (`Station._client_ip`, the trusted-proxy
    chain, SPLIT_PLAN §3.2), not the socket peer: behind the gateway every
    request arrives from the same peer, which would put the whole station in
    one bucket. Each app gets its own window; the public app is the only one
    a browser reaches.
    """
    _rate: dict[str, deque[float]] = {}
    limit = limit or station.cfg.requests.per_client_per_10min

    def _rate_limited(request: Request) -> bool:
        host = (station._client_ip(request)
                or (request.client.host if request.client else None) or "unknown")
        now = time.monotonic()
        window = 10 * 60.0
        dq = _rate.setdefault(host, deque(maxlen=limit))
        # drop timestamps outside the 10-minute window
        while dq and now - dq[0] > window:
            dq.popleft()
        if len(dq) >= limit:
            return True
        dq.append(now)
        return False

    return _rate_limited


class FreeRequestGate:
    """One accepted free request per client per `interval_s` (RADIO §14).

    Keyed on the resolved client IP like the attempt limiter above. `reserve`
    answers the seconds still to wait (0 = go ahead, slot taken); a request
    that is then refused calls `release`, so a rejected or failed attempt
    never costs the listener their slot. In-memory per process, like the
    other request-line limits. `interval_s <= 0` disables it.
    """

    def __init__(self, interval_s: float,
                 now: Callable[[], float] = time.monotonic) -> None:
        self.interval_s = interval_s
        self._now = now
        self._last: dict[str, float] = {}

    def reserve(self, client: str) -> float:
        if self.interval_s <= 0:
            return 0.0
        now = self._now()
        if len(self._last) > 10000:   # forget expired clients
            self._last = {k: t for k, t in self._last.items()
                          if now - t < self.interval_s}
        last = self._last.get(client)
        if last is not None and now - last < self.interval_s:
            return self.interval_s - (now - last)
        self._last[client] = now
        return 0.0

    def release(self, client: str) -> None:
        self._last.pop(client, None)


def _minutes(seconds: float) -> str:
    n = max(1, math.ceil(seconds / 60.0))
    return f"{n} minute{'' if n == 1 else 's'}"
