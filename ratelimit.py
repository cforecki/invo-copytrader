"""Sliding-window rate limiter for the Invo API.

Invo's published budget is 250 requests / 5 minutes per IP (a Cloudflare
fixed window). We default to a lower in-process budget (INVO_RATE_LIMIT,
default 220) so a refresh call, a retry, or the app open in your browser on
the same IP doesn't tip us over. NOTE: the budget is per *IP*, not per
process -- if you run the screener and the bot at the same time, split the
budget between them (e.g. INVO_RATE_LIMIT=110 in each).
"""

from __future__ import annotations

import collections
import threading
import time


class SlidingWindowLimiter:
    def __init__(self, max_calls: int, window_s: float, clock=time.monotonic, sleep=time.sleep):
        if max_calls <= 0 or window_s <= 0:
            raise ValueError("max_calls and window_s must be positive")
        self.max_calls = max_calls
        self.window_s = window_s
        self._clock = clock
        self._sleep = sleep
        self._calls: collections.deque[float] = collections.deque()
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Block until a call is allowed. Returns seconds spent waiting."""
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                while self._calls and now - self._calls[0] >= self.window_s:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return waited
                delay = self.window_s - (now - self._calls[0]) + 0.01
            self._sleep(delay)
            waited += delay

    def used(self) -> int:
        with self._lock:
            now = self._clock()
            return sum(1 for t in self._calls if now - t < self.window_s)
