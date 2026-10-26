from unittest.mock import MagicMock

import pytest

from saltext.bmc.states import bmc as bmc_state
from saltext.bmc.utils import wait as wait_util


@pytest.fixture
def fake_salt():
    return {
        "bmc.power_status": MagicMock(return_value="on"),
        "bmc.power_on": MagicMock(return_value={"action": "On", "result": True}),
        "bmc.power_off": MagicMock(return_value={"action": "GracefulShutdown", "result": True}),
        "bmc.power_reset": MagicMock(return_value={"action": "GracefulRestart", "result": True}),
        "bmc.wait_for_power": MagicMock(
            return_value={
                "result": True,
                "state": "on",
                "target": "on",
                "polls": 2,
                "elapsed": 7.0,
                "error": None,
            }
        ),
        "bmc.get_boot_device": MagicMock(
            return_value={"device": "none", "redfish_target": "None", "enabled": "Disabled"}
        ),
        "bmc.set_boot_device": MagicMock(return_value={"result": True}),
    }


@pytest.fixture(autouse=True)
def _inject(monkeypatch, fake_salt):
    monkeypatch.setattr(bmc_state, "__salt__", fake_salt, raising=False)
    monkeypatch.setattr(bmc_state, "__opts__", {"test": False}, raising=False)
    # Skip the real initial_delay sleep in rebooted tests.
    monkeypatch.setattr(bmc_state.time, "sleep", lambda *_: None)


def test_powered_noop_when_already_on(fake_salt):
    fake_salt["bmc.power_status"].return_value = "on"
    ret = bmc_state.powered("test-host", "on")
    assert ret["result"] is True
    assert ret["changes"] == {}
    assert "already on" in ret["comment"]
    fake_salt["bmc.power_on"].assert_not_called()


def test_powered_changes_when_off(fake_salt):
    fake_salt["bmc.power_status"].return_value = "off"
    ret = bmc_state.powered("test-host", "on")
    assert ret["result"] is True
    assert ret["changes"] == {"power": {"old": "off", "new": "on"}}
    fake_salt["bmc.power_on"].assert_called_once_with("test-host")


def test_powered_test_mode_reports_change(monkeypatch, fake_salt):
    monkeypatch.setattr(bmc_state, "__opts__", {"test": True}, raising=False)
    fake_salt["bmc.power_status"].return_value = "off"
    ret = bmc_state.powered("test-host", "on")
    assert ret["result"] is None
    assert ret["changes"] == {"power": {"old": "off", "new": "on"}}
    fake_salt["bmc.power_on"].assert_not_called()


def test_powered_accepts_bool(fake_salt):
    fake_salt["bmc.power_status"].return_value = "off"
    ret = bmc_state.powered("test-host", True)
    assert ret["result"] is True
    fake_salt["bmc.power_on"].assert_called_once()


def test_powered_rejects_bad_value():
    ret = bmc_state.powered("test-host", "maybe")
    assert ret["result"] is False
    assert "Unknown power value" in ret["comment"]


def test_boot_device_noop_when_already_set(fake_salt):
    fake_salt["bmc.get_boot_device"].return_value = {
        "device": "http",
        "redfish_target": "UefiHttp",
        "enabled": "Once",
    }
    ret = bmc_state.boot_device("test-host", "http", persistent=False)
    assert ret["result"] is True
    assert ret["changes"] == {}
    fake_salt["bmc.set_boot_device"].assert_not_called()


def test_boot_device_changes(fake_salt):
    fake_salt["bmc.get_boot_device"].return_value = {
        "device": "none",
        "redfish_target": "None",
        "enabled": "Disabled",
    }
    ret = bmc_state.boot_device("test-host", "http", persistent=False)
    assert ret["result"] is True
    assert ret["changes"]["device"] == {"old": "none", "new": "http"}
    fake_salt["bmc.set_boot_device"].assert_called_once_with(
        "test-host", device="http", persistent=False
    )


def test_boot_device_test_mode(monkeypatch, fake_salt):
    monkeypatch.setattr(bmc_state, "__opts__", {"test": True}, raising=False)
    fake_salt["bmc.get_boot_device"].return_value = {
        "device": "disk",
        "redfish_target": "Hdd",
        "enabled": "Continuous",
    }
    ret = bmc_state.boot_device("test-host", "http", persistent=False)
    assert ret["result"] is None
    assert ret["changes"]["device"] == {"old": "disk", "new": "http"}
    fake_salt["bmc.set_boot_device"].assert_not_called()


# ----------------------------------------------------------------------
# bmc.rebooted
# ----------------------------------------------------------------------


def test_rebooted_success_bmc_only(fake_salt):
    ret = bmc_state.rebooted("test-host", timeout=60, initial_delay=0)
    assert ret["result"] is True
    assert "rebooted" in ret["comment"]
    assert ret["changes"]["power"] == {"old": "reset", "new": "on"}
    fake_salt["bmc.power_reset"].assert_called_once_with("test-host", force=False)
    fake_salt["bmc.wait_for_power"].assert_called_once()


def test_rebooted_force(fake_salt):
    bmc_state.rebooted("test-host", force=True, initial_delay=0)
    fake_salt["bmc.power_reset"].assert_called_once_with("test-host", force=True)


def test_rebooted_test_mode(monkeypatch, fake_salt):
    monkeypatch.setattr(bmc_state, "__opts__", {"test": True}, raising=False)
    ret = bmc_state.rebooted("test-host", os_host="10.0.0.5", os_port=22)
    assert ret["result"] is None
    assert "Would reset" in ret["comment"]
    assert "10.0.0.5:22" in ret["comment"]
    fake_salt["bmc.power_reset"].assert_not_called()


def test_rebooted_power_reset_failure_short_circuits(fake_salt):
    fake_salt["bmc.power_reset"].side_effect = RuntimeError("boom")
    ret = bmc_state.rebooted("test-host", initial_delay=0)
    assert ret["result"] is False
    assert "boom" in ret["comment"]
    fake_salt["bmc.wait_for_power"].assert_not_called()


def test_rebooted_power_wait_timeout(fake_salt):
    fake_salt["bmc.wait_for_power"].return_value = {
        "result": False,
        "state": "off",
        "target": "on",
        "polls": 12,
        "elapsed": 60.0,
        "error": None,
    }
    ret = bmc_state.rebooted("test-host", timeout=60, initial_delay=0)
    assert ret["result"] is False
    assert "did not report power 'on'" in ret["comment"]
    assert ret["changes"]["power"]["new"] == "off"


def test_rebooted_with_os_probe_success(monkeypatch):
    monkeypatch.setattr(wait_util, "tcp_probe", lambda *_a, **_k: True)
    # Make poll_until deterministic without sleeping.
    monkeypatch.setattr(wait_util.time, "monotonic", lambda: 0)
    monkeypatch.setattr(wait_util.time, "sleep", lambda *_: None)

    ret = bmc_state.rebooted(
        "test-host",
        initial_delay=0,
        os_host="10.0.0.5",
        os_port=22,
        os_timeout=30,
    )
    assert ret["result"] is True
    assert ret["changes"]["os_probe"]["reachable"] is True
    assert ret["changes"]["os_probe"]["host"] == "10.0.0.5"


def test_rebooted_with_os_probe_timeout(monkeypatch):
    monkeypatch.setattr(wait_util, "tcp_probe", lambda *_a, **_k: False)
    clock = {"t": 0.0}
    monkeypatch.setattr(wait_util.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(wait_util.time, "sleep", lambda s: clock.update(t=clock["t"] + s))

    ret = bmc_state.rebooted(
        "test-host",
        initial_delay=0,
        os_host="10.0.0.5",
        os_port=22,
        os_timeout=5,
        os_interval=1,
    )
    assert ret["result"] is False
    assert "not reachable" in ret["comment"]
    assert ret["changes"]["os_probe"]["reachable"] is False
    # BMC power change still recorded.
    assert ret["changes"]["power"] == {"old": "reset", "new": "on"}
