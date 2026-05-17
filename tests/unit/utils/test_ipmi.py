"""Tests for saltext.bmc.utils.ipmi (mocks pyghmi entirely)."""

import importlib.util
import sys
from unittest.mock import MagicMock

import pytest

from saltext.bmc.utils import ipmi as ipmi_util

# Tests that exercise the FRU byte-parsing code path need the real
# ``pyghmi.ipmi.fru`` module (we use pyghmi's FRU parser).  Other tests
# work fine with a fully-mocked pyghmi.
HAS_PYGHMI = importlib.util.find_spec("pyghmi") is not None
requires_pyghmi = pytest.mark.skipif(
    not HAS_PYGHMI, reason="pyghmi not installed (optional ipmi extra)"
)


@pytest.fixture
def fake_pyghmi(monkeypatch):
    """
    Install a fake ``pyghmi.ipmi.command`` module so IpmiClient can import it
    without the real package present.  Returns the fake Command class.

    If real ``pyghmi`` is installed, ``pyghmi.ipmi.fru`` is left as the real
    module so FRU byte-parsing tests can use pyghmi's parser.
    """
    real_fru = None
    if HAS_PYGHMI:
        # pylint: disable=import-outside-toplevel,import-error
        import pyghmi.ipmi.fru as real_fru

    fake_cmd_cls = MagicMock(name="Command")
    fake_module = MagicMock()
    fake_module.Command = fake_cmd_cls
    fake_pkg = MagicMock()
    fake_pkg.ipmi = MagicMock()
    fake_pkg.ipmi.command = fake_module
    monkeypatch.setitem(sys.modules, "pyghmi", fake_pkg)
    monkeypatch.setitem(sys.modules, "pyghmi.ipmi", fake_pkg.ipmi)
    monkeypatch.setitem(sys.modules, "pyghmi.ipmi.command", fake_module)
    if real_fru is not None:
        fake_pkg.ipmi.fru = real_fru
        monkeypatch.setitem(sys.modules, "pyghmi.ipmi.fru", real_fru)
    return fake_cmd_cls


def _client():
    return ipmi_util.IpmiClient(host="10.0.0.5", username="root", password="calvin")


def test_power_status_returns_on(fake_pyghmi):
    fake_pyghmi.return_value.get_power.return_value = {"powerstate": "on"}
    with _client() as c:
        assert c.power_status() == "on"


def test_power_status_returns_off(fake_pyghmi):
    fake_pyghmi.return_value.get_power.return_value = {"powerstate": "off"}
    with _client() as c:
        assert c.power_status() == "off"


def test_power_status_unknown(fake_pyghmi):
    fake_pyghmi.return_value.get_power.return_value = {"powerstate": "unknown"}
    with _client() as c:
        assert c.power_status() == "unknown"


@pytest.mark.parametrize(
    "reset_type,expected_arg",
    [
        ("On", "on"),
        ("ForceOff", "off"),
        ("GracefulShutdown", "shutdown"),
        ("GracefulRestart", "reset"),
        ("ForceRestart", "reset"),
    ],
)
def test_do_reset_translates_to_pyghmi(fake_pyghmi, reset_type, expected_arg):
    instance = fake_pyghmi.return_value
    with _client() as c:
        result = c.do_reset(reset_type)
    assert result == {"action": reset_type, "result": True}
    instance.set_power.assert_called_once_with(expected_arg)


def test_do_reset_power_cycle_issues_raw_command(fake_pyghmi):
    instance = fake_pyghmi.return_value
    with _client() as c:
        result = c.do_reset("PowerCycle")
    assert result == {"action": "PowerCycle", "result": True}
    instance.raw_command.assert_called_once_with(netfn=0x00, command=0x02, data=[0x02])
    instance.set_power.assert_not_called()


def test_do_reset_rejects_unknown(fake_pyghmi):
    with _client() as c:
        with pytest.raises(ipmi_util.IpmiError):
            c.do_reset("WarpDrive")


def test_get_boot_translates_pyghmi_response(fake_pyghmi):
    instance = fake_pyghmi.return_value
    instance.get_bootdev.return_value = {"bootdev": "network", "persistent": False}
    with _client() as c:
        result = c.get_boot()
    assert result["device"] == "pxe"
    assert result["native_target"] == "network"
    assert result["enabled"] == "Once"
    assert result["redfish_target"] is None


def test_get_boot_persistent_is_continuous(fake_pyghmi):
    instance = fake_pyghmi.return_value
    instance.get_bootdev.return_value = {"bootdev": "hd", "persistent": True}
    with _client() as c:
        result = c.get_boot()
    assert result["device"] == "disk"
    assert result["enabled"] == "Continuous"


def test_get_boot_default_is_disabled(fake_pyghmi):
    instance = fake_pyghmi.return_value
    instance.get_bootdev.return_value = {"bootdev": "default", "persistent": False}
    with _client() as c:
        result = c.get_boot()
    assert result["device"] == "none"
    assert result["enabled"] == "Disabled"


@pytest.mark.parametrize(
    "device,expected_native",
    [
        ("disk", "hd"),
        ("pxe", "network"),
        ("bios", "setup"),
        ("cd", "cd"),
        ("none", "default"),
    ],
)
def test_set_boot_supported_devices(fake_pyghmi, device, expected_native):
    instance = fake_pyghmi.return_value
    with _client() as c:
        result = c.set_boot(device, persistent=False)
    assert result["native_target"] == expected_native
    instance.set_bootdev.assert_called_once_with(expected_native, persist=False, uefiboot=False)


def test_set_boot_persistent_flag(fake_pyghmi):
    instance = fake_pyghmi.return_value
    with _client() as c:
        c.set_boot("pxe", persistent=True)
    instance.set_bootdev.assert_called_once_with("network", persist=True, uefiboot=False)


@pytest.mark.parametrize("device", ["http", "usb"])
def test_set_boot_rejects_unsupported(fake_pyghmi, device):
    with _client() as c:
        with pytest.raises(ipmi_util.IpmiError):
            c.set_boot(device)


def test_auth_error_is_translated(fake_pyghmi):
    fake_pyghmi.side_effect = RuntimeError("Unauthorized credentials")
    with pytest.raises(ipmi_util.IpmiAuthError):
        with _client():
            pass


def test_other_errors_become_ipmi_error(fake_pyghmi):
    fake_pyghmi.side_effect = OSError("connection refused")
    with pytest.raises(ipmi_util.IpmiError):
        with _client():
            pass


def test_op_failure_inside_session_becomes_ipmi_error(fake_pyghmi):
    instance = fake_pyghmi.return_value
    instance.get_power.side_effect = RuntimeError("timeout reading sensor")
    with _client() as c:
        with pytest.raises(ipmi_util.IpmiError):
            c.power_status()


def test_op_auth_failure_inside_session_becomes_auth_error(fake_pyghmi):
    instance = fake_pyghmi.return_value
    instance.set_power.side_effect = RuntimeError("Invalid credentials")
    with _client() as c:
        with pytest.raises(ipmi_util.IpmiAuthError):
            c.do_reset("On")


# ----------------------------------------------------------------------
# get_system_info — exercised via raw_command FRU mocks
# ----------------------------------------------------------------------

# A valid 72-byte FRU image with only a product area (offset 8).
# Decoded by pyghmi.ipmi.fru.FRU into:
#   Manufacturer: Supermicro
#   Product name: X9DR3-LN4F+
#   Model: 0123456789AB
#   Hardware Version: 1.10
#   Serial Number: S12345
#   Asset Number: AT01
#   FRU ID: FRU0
_FRU_BYTES = bytes.fromhex(
    "01000000010000fe010800ca53757065726d6963726f"
    "cb58394452332d4c4e34462bcc303132333435363738"
    "394142c4312e3130c6533132333435c441543031c446"
    "525530c1001f"
)


def _make_fru_raw_command(reject_above: int | None = None):
    """
    Return a side_effect for ``raw_command`` that serves ``_FRU_BYTES``.

    If ``reject_above`` is set, any FRU read with a chunksize larger than
    that value returns IPMI completion code ``0xC7`` ("Request data length
    invalid") — the failure mode seen on legacy Supermicro BMCs.
    """

    def raw(*, netfn, command, data):
        if netfn == 0x0A and command == 0x10:  # Get FRU Inventory Area Info
            size = len(_FRU_BYTES)
            return {"code": 0, "data": bytearray([size & 0xFF, size >> 8, 0])}
        if netfn == 0x0A and command == 0x11:  # Read FRU Data
            _, off_lo, off_hi, n = data
            if reject_above is not None and n > reject_above:
                return {
                    "code": 0xC7,
                    "data": bytearray(),
                    "error": "Request data length invalid",
                }
            offset = off_lo | (off_hi << 8)
            chunk = _FRU_BYTES[offset : offset + n]
            return {"code": 0, "data": bytearray([len(chunk)]) + bytearray(chunk)}
        return {"code": 0xC1, "data": bytearray(), "error": "unsupported"}

    return raw


@requires_pyghmi
def test_get_system_info_reads_fru_via_raw_command(fake_pyghmi):
    instance = fake_pyghmi.return_value
    instance.raw_command.side_effect = _make_fru_raw_command()
    instance.get_firmware.return_value = iter([("BMC Version", {"version": "3.5"})])
    instance.get_power.return_value = {"powerstate": "on"}
    with _client() as c:
        info = c.get_system_info()
    assert info["manufacturer"] == "Supermicro"
    assert info["model"] == "X9DR3-LN4F+"
    assert info["serial_number"] == "S12345"
    assert info["firmware_version"] == "3.5"
    assert info["power_state"] == "on"
    assert info["host_name"] is None
    assert info["bios_version"] is None


@requires_pyghmi
def test_get_system_info_chunksize_backoff_on_0xc7(fake_pyghmi):
    """Legacy BMC that returns 0xC7 for >16-byte FRU reads should still parse."""
    instance = fake_pyghmi.return_value
    instance.raw_command.side_effect = _make_fru_raw_command(reject_above=16)
    instance.get_firmware.return_value = iter([("BMC Version", {"version": "1.26"})])
    instance.get_power.return_value = {"powerstate": "on"}
    with _client() as c:
        info = c.get_system_info()
    assert info["manufacturer"] == "Supermicro"
    assert info["model"] == "X9DR3-LN4F+"
    assert info["serial_number"] == "S12345"
    assert info["firmware_version"] == "1.26"
    # Verify back-off actually happened: at least one 0xC7 was returned and
    # then a smaller chunk request followed.  raw_command was called many
    # times (1 area-info + N reads).
    assert instance.raw_command.call_count > 5


def test_get_system_info_fru_absent_returns_none_product_fields(fake_pyghmi):
    instance = fake_pyghmi.return_value

    def raw(*, netfn, command, data):  # pylint: disable=unused-argument
        # FRU device not present.
        return {"code": 0xCB, "data": bytearray(), "error": "FRU device not present"}

    instance.raw_command.side_effect = raw
    instance.get_firmware.return_value = iter([("BMC Version", {"version": "2.0a"})])
    instance.get_power.return_value = {"powerstate": "off"}
    with _client() as c:
        info = c.get_system_info()
    assert info["manufacturer"] is None
    assert info["model"] is None
    assert info["serial_number"] is None
    assert info["uuid"] is None
    assert info["firmware_version"] == "2.0a"
    assert info["power_state"] == "off"


@requires_pyghmi
def test_get_system_info_firmware_failure_returns_none_version(fake_pyghmi):
    instance = fake_pyghmi.return_value
    instance.raw_command.side_effect = _make_fru_raw_command()
    instance.get_firmware.side_effect = RuntimeError("oem firmware unsupported")
    instance.get_power.return_value = {"powerstate": "on"}
    with _client() as c:
        info = c.get_system_info()
    assert info["manufacturer"] == "Supermicro"
    assert info["firmware_version"] is None
    assert info["power_state"] == "on"


def test_get_system_info_picks_bmc_entry_from_multi_component_firmware(fake_pyghmi):
    instance = fake_pyghmi.return_value
    instance.raw_command.side_effect = lambda **_: {
        "code": 0xCB,
        "data": bytearray(),
        "error": "absent",
    }
    instance.get_firmware.return_value = iter(
        [
            ("BIOS", {"version": "1.2.3"}),
            ("BMC Active", {"version": "5.10"}),
            ("BMC Backup", {"version": "5.09"}),
        ]
    )
    instance.get_power.return_value = {"powerstate": "on"}
    with _client() as c:
        info = c.get_system_info()
    # First entry whose name contains 'bmc' wins.
    assert info["firmware_version"] == "5.10"


def test_get_system_info_all_fail_returns_all_none(fake_pyghmi):
    instance = fake_pyghmi.return_value
    instance.raw_command.side_effect = RuntimeError("fru broken")
    instance.get_firmware.side_effect = RuntimeError("fw broken")
    instance.get_power.side_effect = RuntimeError("power broken")
    with _client() as c:
        info = c.get_system_info()
    for key in (
        "manufacturer",
        "model",
        "serial_number",
        "uuid",
        "sku",
        "host_name",
        "bios_version",
        "firmware_version",
        "power_state",
    ):
        assert info[key] is None, f"expected {key} to be None"


# ----------------------------------------------------------------------
# _normalize_fru_flat / _normalize_fru_nested — pure dict transforms
# ----------------------------------------------------------------------


def test_normalize_fru_flat_pyghmi_keys():
    fru = {
        "Manufacturer": "Supermicro",
        "Product name": "X9DR3-LN4F+",
        "Model": "0123456789AB",
        "Serial Number": "S12345",
        "UUID": "00000000-0000-0000-0000-0123456789AB",
        "SKU": "SKU-42",
    }
    out = ipmi_util._normalize_fru_flat(fru)
    assert out == {
        "manufacturer": "Supermicro",
        "model": "X9DR3-LN4F+",
        "serial_number": "S12345",
        "uuid": "00000000-0000-0000-0000-0123456789AB",
        "sku": "SKU-42",
    }


def test_normalize_fru_flat_snake_case_keys():
    fru = {
        "product_manufacturer": "Supermicro",
        "product_name": "X9DR3-LN4F+",
        "product_serial": "S12345",
    }
    out = ipmi_util._normalize_fru_flat(fru)
    assert out["manufacturer"] == "Supermicro"
    assert out["model"] == "X9DR3-LN4F+"
    assert out["serial_number"] == "S12345"


def test_normalize_fru_nested_legacy_shape():
    fru = {
        "product": {
            "manufacturer": "Dell",
            "product_name": "PowerEdge R640",
            "serial_number": "ABCD123",
        },
        "uuid": "11111111-2222-3333-4444-555555555555",
    }
    out = ipmi_util._normalize_fru_nested(fru)
    assert out["manufacturer"] == "Dell"
    assert out["model"] == "PowerEdge R640"
    assert out["serial_number"] == "ABCD123"
    assert out["uuid"] == "11111111-2222-3333-4444-555555555555"
