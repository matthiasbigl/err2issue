"""Storm suppression: keep a crash loop from flooding GitHub.

This is a throttle, not a dedup mechanism. It is in-memory and lost on restart —
which is safe here precisely because authoritative dedup reads a strongly
consistent endpoint and creation is guarded by a distributed mutex
(see CHALLENGE.md §1, §4, §6). A lost window costs one extra API round-trip and
an occurrence comment, not a duplicate issue.

Three independent limits, checked in order:

1. per-fingerprint window  — one filing per error per `window_seconds`
2. global rate             — token bucket over all fingerprints
3. new-fingerprint budget  — daily ceiling on *distinct* errors (bad-deploy guard)
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

# Bound the fingerprint table so an adversarial or pathological input stream
# cannot grow it without limit.
MAX_TRACKED_FINGERPRINTS = 10_000


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


ALLOW = Decision(True)


class Suppressor:
    def __init__(
        self,
        window_seconds: int = 600,
        max_per_minute: int = 30,
        max_new_per_day: int = 50,
        clock=time.monotonic,
        wall_clock=time.time,
    ):
        self.window_seconds = window_seconds
        self.max_per_minute = max_per_minute
        self.max_new_per_day = max_new_per_day
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = threading.Lock()

        self._last_seen: OrderedDict[str, float] = OrderedDict()
        self._known: OrderedDict[str, float] = OrderedDict()
        self._bucket = float(max_per_minute)
        self._bucket_updated = clock()
        self._new_today = 0
        self._day = self._current_day()

    def _current_day(self) -> int:
        return int(self._wall_clock() // 86_400)

    def _refill(self, now: float) -> None:
        elapsed = now - self._bucket_updated
        if elapsed <= 0:
            return
        self._bucket = min(
            float(self.max_per_minute), self._bucket + elapsed * (self.max_per_minute / 60.0)
        )
        self._bucket_updated = now

    def _roll_day(self) -> None:
        today = self._current_day()
        if today != self._day:
            self._day = today
            self._new_today = 0

    def check(self, fingerprint: str) -> Decision:
        """Decide whether this occurrence should be filed. Consumes budget when allowed."""
        with self._lock:
            now = self._clock()
            self._roll_day()

            last = self._last_seen.get(fingerprint)
            if last is not None and (now - last) < self.window_seconds:
                remaining = int(self.window_seconds - (now - last))
                return Decision(False, f"within suppression window ({remaining}s remaining)")

            is_new = fingerprint not in self._known
            if is_new and self._new_today >= self.max_new_per_day:
                return Decision(
                    False,
                    f"daily new-fingerprint budget exhausted "
                    f"({self._new_today}/{self.max_new_per_day})",
                )

            self._refill(now)
            if self._bucket < 1.0:
                return Decision(False, f"global rate cap ({self.max_per_minute}/min)")

            self._bucket -= 1.0
            self._last_seen[fingerprint] = now
            self._last_seen.move_to_end(fingerprint)
            if is_new:
                self._known[fingerprint] = now
                self._new_today += 1
            self._known.move_to_end(fingerprint)

            while len(self._last_seen) > MAX_TRACKED_FINGERPRINTS:
                self._last_seen.popitem(last=False)
            while len(self._known) > MAX_TRACKED_FINGERPRINTS:
                self._known.popitem(last=False)

            return ALLOW

    def stats(self) -> dict[str, float | int]:
        with self._lock:
            return {
                "tracked_fingerprints": len(self._last_seen),
                "known_fingerprints": len(self._known),
                "new_today": self._new_today,
                "tokens_available": round(self._bucket, 2),
            }
