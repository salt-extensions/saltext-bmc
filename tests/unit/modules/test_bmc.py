import json

import pytest
import responses

from saltext.bmc.modules import bmc as bmc_mod
from tests.conftest import REDFISH_BASE
from tests.conftest import REDFISH_RESET_PATH
from tests.conftest import REDFISH_SYS_PATH


@pytest.fixture(autouse=True)
def _inject_opts(monkeypatch, bmc_opts):
    monkeypatch.setattr(bmc_mod, "__opts__", bmc_opts, raising=False)


def _reset_body(rsps):
    """Capture the JSON body of the most recent POST to the reset endpoint."""
    for call in rsps.calls:
        if call.request.url.endswith(REDFISH_RESET_PATH):
            return json.loads(call.request.body)
    raise AssertionError("no POST to reset action observed")


def _patch_body(rsps):
    for call in rsps.calls:
        if call.request.method == "PATCH" and call.request.url.endswith(REDFISH_SYS_PATH):
            return json.loads(call.request.body)
    raise AssertionError("no PATCH to system observed")


def test_power_status_on(mocked_redfish_full):
    assert bmc_mod.power_status("test-host") == "on"


def test_power_status_off(mocked_redfish, system_doc, register_systems):
    register_systems(mocked_redfish, system=system_doc(power_state="Off"))
    assert bmc_mod.power_status("test-host") == "off"


def test_power_status_unknown(mocked_redfish, system_doc, register_systems):
    register_systems(mocked_redfish, system=system_doc(power_state=""))
    assert bmc_mod.power_status("test-host") == "unknown"


def test_power_on_posts_correct_reset_type(mocked_redfish_full):
    mocked_redfish_full.add(responses.POST, f"{REDFISH_BASE}{REDFISH_RESET_PATH}", status=204)
    result = bmc_mod.power_on("test-host")
    assert result["action"] == "On"
    assert _reset_body(mocked_redfish_full) == {"ResetType": "On"}


def test_power_off_graceful_by_default(mocked_redfish_full):
    mocked_redfish_full.add(responses.POST, f"{REDFISH_BASE}{REDFISH_RESET_PATH}", status=204)
    bmc_mod.power_off("test-host")
    assert _reset_body(mocked_redfish_full) == {"ResetType": "GracefulShutdown"}


def test_power_off_force(mocked_redfish_full):
    mocked_redfish_full.add(responses.POST, f"{REDFISH_BASE}{REDFISH_RESET_PATH}", status=204)
    bmc_mod.power_off("test-host", force=True)
    assert _reset_body(mocked_redfish_full) == {"ResetType": "ForceOff"}


def test_power_cycle(mocked_redfish_full):
    mocked_redfish_full.add(responses.POST, f"{REDFISH_BASE}{REDFISH_RESET_PATH}", status=204)
    bmc_mod.power_cycle("test-host")
    assert _reset_body(mocked_redfish_full) == {"ResetType": "PowerCycle"}


def test_power_reset_graceful_by_default(mocked_redfish_full):
    mocked_redfish_full.add(responses.POST, f"{REDFISH_BASE}{REDFISH_RESET_PATH}", status=204)
    bmc_mod.power_reset("test-host")
    assert _reset_body(mocked_redfish_full) == {"ResetType": "GracefulRestart"}


def test_get_boot_device_parses_system(mocked_redfish, system_doc, register_systems):
    register_systems(
        mocked_redfish,
        system=system_doc(boot_target="UefiHttp", boot_enabled="Once"),
    )
    result = bmc_mod.get_boot_device("test-host")
    assert result["device"] == "http"
    assert result["redfish_target"] == "UefiHttp"
    assert result["native_target"] == "UefiHttp"
    assert result["enabled"] == "Once"


def test_set_boot_device_http_once(mocked_redfish_full):
    mocked_redfish_full.add(responses.PATCH, f"{REDFISH_BASE}{REDFISH_SYS_PATH}", status=204)
    result = bmc_mod.set_boot_device("test-host", device="http", persistent=False)
    assert result["redfish_target"] == "UefiHttp"
    assert result["enabled"] == "Once"
    assert _patch_body(mocked_redfish_full) == {
        "Boot": {
            "BootSourceOverrideEnabled": "Once",
            "BootSourceOverrideTarget": "UefiHttp",
        }
    }


def test_set_boot_device_pxe_persistent(mocked_redfish_full):
    mocked_redfish_full.add(responses.PATCH, f"{REDFISH_BASE}{REDFISH_SYS_PATH}", status=204)
    bmc_mod.set_boot_device("test-host", device="pxe", persistent=True)
    assert _patch_body(mocked_redfish_full) == {
        "Boot": {
            "BootSourceOverrideEnabled": "Continuous",
            "BootSourceOverrideTarget": "Pxe",
        }
    }


def test_set_boot_device_rejects_unknown():
    with pytest.raises(ValueError):
        bmc_mod.set_boot_device("test-host", device="floppy")


def test_explicit_kwargs_override_pillar(mocked_redfish, system_doc, register_systems):
    register_systems(mocked_redfish)
    # Provide explicit creds with no pillar profile match — verifies overrides.
    bmc_mod.__opts__ = {"pillar": {}}
    result = bmc_mod.power_status(
        host="bmc.test",
        username="root",
        password="calvin",
        verify_ssl=False,
        backend="redfish",
    )
    assert result == "on"
