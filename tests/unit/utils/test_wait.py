"""Tests for the polling helper used by wait_for_power / rebooted."""

from unittest.mock import MagicMock

import pytest

from saltext.bmc.utils import wait as wait_util


@pytest.fixture(autouse=True)
def _fast_clock(monkeypatch):
    """Replace time.sleep with a no-op and drive monotonic from a counter.

    Each call advances by 1 second.  Tests pass intervals/timeouts in those
    units so behaviour is deterministic without sleeping.
    """
    clock = {"t": 0.0}

    def fake_monotonic():
        return clock["t"]

    def fake_sleep(seconds):
        clock["t"] += seconds

    monkeypatch.setattr(wait_util.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(wait_util.time, "sleep", fake_sleep)
    return clock


def test_poll_until_succeeds_first_try():
    fn = MagicMock(return_value="on")
    ok, last, polls, elapsed = wait_util.poll_until(
        fn=fn, predicate=lambda v: v == "on", timeout=10, interval=1
    )
    assert ok is True
    assert last == "on"
    assert polls == 1
    assert elapsed == 0
    fn.assert_called_once()


def test_poll_until_succeeds_after_retries():
    seq = iter(["off", "off", "on"])
    ok, last, polls, _ = wait_util.poll_until(
        fn=lambda: next(seq), predicate=lambda v: v == "on", timeout=10, interval=1
    )
    assert ok is True
    assert last == "on"
    assert polls == 3


def test_poll_until_times_out():
    ok, last, polls, elapsed = wait_util.poll_until(
        fn=lambda: "off", predicate=lambda v: v == "on", timeout=3, interval=1
    )
    assert ok is False
    assert last == "off"
    # First call at t=0, sleep 1, call at 1, sleep 1, call at 2, sleep 1, call at 3, elapsed>=timeout.
    assert polls == 4
    assert elapsed >= 3


def test_poll_until_catches_exceptions_and_keeps_trying():
    calls = [RuntimeError("transient"), RuntimeError("transient"), "on"]

    def fn():
        v = calls.pop(0)
        if isinstance(v, Exception):
            raise v
        return v

    ok, last, polls, _ = wait_util.poll_until(
        fn=fn, predicate=lambda v: v == "on", timeout=10, interval=1
    )
    assert ok is True
    assert last == "on"
    assert polls == 3


def test_poll_until_returns_last_exception_on_timeout():
    def fn():
        raise RuntimeError("nope")

    ok, last, polls, _ = wait_util.poll_until(
        fn=fn, predicate=lambda v: v == "on", timeout=2, interval=1
    )
    assert ok is False
    assert isinstance(last, RuntimeError)
    assert polls >= 1


def test_tcp_probe_success(monkeypatch):
    fake_sock = MagicMock()
    fake_sock.__enter__ = MagicMock(return_value=fake_sock)
    fake_sock.__exit__ = MagicMock(return_value=False)
    monkeypatch.setattr(wait_util.socket, "create_connection", lambda *a, **k: fake_sock)
    assert wait_util.tcp_probe("10.0.0.1", 22) is True


def test_tcp_probe_failure(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("refused")

    monkeypatch.setattr(wait_util.socket, "create_connection", boom)
    assert wait_util.tcp_probe("10.0.0.1", 22) is False
