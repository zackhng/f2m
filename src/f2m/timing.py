"""Step timers.

Wrap a step in ``with timed("name"):``. Nested steps get path names such as
``parse/pdf/tables``, so a report shows where the time goes. Times add up when
a step runs many times (e.g. once per page). Outside ``recording()`` the timers
do nothing, so library code can call them freely.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class Timings:
    seconds: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    calls: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    _stack: list[str] = field(default_factory=list)

    def ms(self) -> dict[str, float]:
        """Step path -> total milliseconds, in the order steps first ran."""
        return {k: round(v * 1000, 3) for k, v in self.seconds.items()}

    def top_level(self) -> dict[str, float]:
        return {k: v for k, v in self.ms().items() if "/" not in k}

    def total_ms(self) -> float:
        return round(sum(v for k, v in self.seconds.items() if "/" not in k) * 1000, 3)


_current: ContextVar[Timings | None] = ContextVar("f2m_timings", default=None)


@contextmanager
def recording() -> Iterator[Timings]:
    t = Timings()
    token = _current.set(t)
    try:
        yield t
    finally:
        _current.reset(token)


@contextmanager
def timed(name: str) -> Iterator[None]:
    t = _current.get()
    if t is None:
        yield
        return
    t._stack.append(name)
    key = "/".join(t._stack)
    start = time.perf_counter()
    try:
        yield
    finally:
        t.seconds[key] += time.perf_counter() - start
        t.calls[key] += 1
        t._stack.pop()
