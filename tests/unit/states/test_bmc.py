from unittest.mock import MagicMock

import pytest

from saltext.bmc.states import bmc as bmc_state


@pytest.fixture
def fake_salt():
    return {
        "bmc.power_status": MagicMock(return_value="on"),
        "bmc.power_on": MagicMock(return_value={"action": "On", "result": True}),
        "bmc.power_off": MagicMock(return_value={"action": "GracefulShutdown", "result": True}),
        "bmc.get_boot_device": MagicMock(
            return_value={"device": "none", "redfish_target": "None", "enabled": "Disabled"}
        ),
        "bmc.set_boot_device": MagicMock(return_value={"result": True}),
    }


@pytest.fixture(autouse=True)
def _inject(monkeypatch, fake_salt):
    monkeypatch.setattr(bmc_state, "__salt__", fake_salt, raising=False)
    monkeypatch.setattr(bmc_state, "__opts__", {"test": False}, raising=False)


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
