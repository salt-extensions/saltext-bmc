"""
Small polling helpers used by ``wait_for_power`` and the ``rebooted`` state.

These deliberately stay framework-agnostic so they can be reused by both
the profile-based ``bmc.*`` module/state and the resource-based
``bmc_host.*`` module/state.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Callable
from typing import Any


def poll_until(
    fn: Callable[[], Any],
    predicate: Callable[[Any], bool],
    timeout: float,
    interval: float,
) -> tuple[bool, Any, int, float]:
    """
    Call ``fn`` repeatedly until ``predicate(fn())`` is True or ``timeout`` elapses.

    Exceptions raised by ``fn`` are caught and treated as a non-matching result;
    the most recent exception is returned in place of the value so the caller
    can surface it.  Sleeps are clamped so the loop never overshoots ``timeout``
    by more than the duration of one ``fn`` call.

    :returns: ``(ok, last_value, polls, elapsed)`` — ``ok`` True if the predicate
              matched within ``timeout``; ``last_value`` is either the final
              value or the final exception; ``polls`` is the call count;
              ``elapsed`` is wall-clock seconds.
    """
    start = time.monotonic()
    polls = 0
    last: Any = None
    while True:
        polls += 1
        try:
            last = fn()
        except Exception as exc:  # pylint: disable=broad-except
            last = exc
        else:
            if predicate(last):
                return True, last, polls, time.monotonic() - start
        elapsed = time.monotonic() - start
        if elapsed >= timeout:
            return False, last, polls, elapsed
        time.sleep(min(interval, max(0.0, timeout - elapsed)))


def tcp_probe(host: str, port: int, connect_timeout: float = 5.0) -> bool:
    """Return ``True`` iff a TCP connection to ``host:port`` succeeds."""
    try:
        with socket.create_connection((host, port), timeout=connect_timeout):
            return True
    except OSError:
        return False
