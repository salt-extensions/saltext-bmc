"""Tests for system_info, sensor_data, and the bmc_redfish passthrough."""

import importlib.util
import json
import sys
from unittest.mock import MagicMock

import pytest
import responses

from saltext.bmc.modules import bmc as bmc_mod
from saltext.bmc.modules import bmc_redfish
from saltext.bmc.utils import redfish as rf
from tests.conftest import REDFISH_BASE
from tests.conftest import REDFISH_SYS_PATH

HAS_PYGHMI = importlib.util.find_spec("pyghmi") is not None
requires_pyghmi = pytest.mark.skipif(
    not HAS_PYGHMI, reason="pyghmi not installed (optional ipmi extra)"
)


@pytest.fixture(autouse=True)
def _inject_opts(monkeypatch, bmc_opts):
    monkeypatch.setattr(bmc_mod, "__opts__", bmc_opts, raising=False)
    monkeypatch.setattr(bmc_redfish, "__opts__", bmc_opts, raising=False)


@pytest.fixture
def fake_pyghmi(monkeypatch):
    """Like the fixture in tests/unit/utils/test_ipmi.py: stub Command, keep
    the real ``pyghmi.ipmi.fru`` module (if installed) so FRU bytes can parse."""
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


# ---------------------------------------------------------------------------
# get_system_info — Redfish
# ---------------------------------------------------------------------------


def test_redfish_system_info(mocked_redfish_full):
    # Override the system doc returned by the default fixture with rich fields.
    mocked_redfish_full.replace(
        responses.GET,
        f"{REDFISH_BASE}{REDFISH_SYS_PATH}",
        json={
            "@odata.id": REDFISH_SYS_PATH,
            "PowerState": "On",
            "Manufacturer": "Dell Inc.",
            "Model": "PowerEdge R740",
            "SerialNumber": "ABC1234",
            "UUID": "12345678-1234-1234-1234-123456789abc",
            "SKU": "SKU-9999",
            "HostName": "esx01.lab",
            "BiosVersion": "2.13.3",
            "Boot": {},
            "Actions": {"#ComputerSystem.Reset": {"target": f"{REDFISH_SYS_PATH}/Actions/Reset"}},
        },
    )
    mocked_redfish_full.add(
        responses.GET,
        f"{REDFISH_BASE}/redfish/v1/Managers",
        json={"Members": [{"@odata.id": "/redfish/v1/Managers/iDRAC"}]},
    )
    mocked_redfish_full.add(
        responses.GET,
        f"{REDFISH_BASE}/redfish/v1/Managers/iDRAC",
        json={"FirmwareVersion": "4.40.00.00"},
    )

    info = bmc_mod.get_system_info("test-host")
    assert info["manufacturer"] == "Dell Inc."
    assert info["model"] == "PowerEdge R740"
    assert info["serial_number"] == "ABC1234"
    assert info["uuid"] == "12345678-1234-1234-1234-123456789abc"
    assert info["sku"] == "SKU-9999"
    assert info["host_name"] == "esx01.lab"
    assert info["bios_version"] == "2.13.3"
    assert info["firmware_version"] == "4.40.00.00"
    assert info["power_state"] == "on"


def test_redfish_system_info_missing_manager_is_none(mocked_redfish_full):
    mocked_redfish_full.add(
        responses.GET,
        f"{REDFISH_BASE}/redfish/v1/Managers",
        json={"Members": []},
    )
    info = bmc_mod.get_system_info("test-host")
    # PowerState='On' comes from the default system doc; firmware_version None.
    assert info["power_state"] == "on"
    assert info["firmware_version"] is None


# ---------------------------------------------------------------------------
# get_system_info — IPMI
# ---------------------------------------------------------------------------


# A valid 72-byte FRU image with only a product area.  See
# tests/unit/utils/test_ipmi.py for the construction; matches pyghmi's
# decoded output exactly so callers can rely on field names.
_FRU_BYTES = bytes.fromhex(
    "01000000010000fe010800ca53757065726d6963726f"
    "cb58394452332d4c4e34462bcc303132333435363738"
    "394142c4312e3130c6533132333435c441543031c446"
    "525530c1001f"
)


def _serve_fru(*, netfn, command, data):
    """raw_command side_effect that serves _FRU_BYTES (no chunksize limits)."""
    if netfn == 0x0A and command == 0x10:
        size = len(_FRU_BYTES)
        return {"code": 0, "data": bytearray([size & 0xFF, size >> 8, 0])}
    if netfn == 0x0A and command == 0x11:
        _, off_lo, off_hi, n = data
        offset = off_lo | (off_hi << 8)
        chunk = _FRU_BYTES[offset : offset + n]
        return {"code": 0, "data": bytearray([len(chunk)]) + bytearray(chunk)}
    return {"code": 0xC1, "data": bytearray(), "error": "unsupported"}


@requires_pyghmi
def test_ipmi_system_info_reads_fru_via_raw_command(monkeypatch, bmc_opts, fake_pyghmi):
    """End-to-end: FRU is read via raw IPMI commands, firmware via get_firmware()."""
    bmc_opts["pillar"]["saltext.bmc"]["profiles"]["test-host"]["backend"] = "ipmi"
    monkeypatch.setattr(bmc_mod, "__opts__", bmc_opts, raising=False)

    instance = fake_pyghmi.return_value
    instance.raw_command.side_effect = _serve_fru
    instance.get_firmware.return_value = iter([("BMC Version", {"version": "1.74"})])
    instance.get_power.return_value = {"powerstate": "on"}

    info = bmc_mod.get_system_info("test-host")
    assert info["manufacturer"] == "Supermicro"
    assert info["model"] == "X9DR3-LN4F+"
    assert info["serial_number"] == "S12345"
    assert info["firmware_version"] == "1.74"
    assert info["power_state"] == "on"
    # Fields IPMI does not expose:
    assert info["host_name"] is None
    assert info["bios_version"] is None


@requires_pyghmi
def test_ipmi_system_info_legacy_bmc_with_small_chunk_size(monkeypatch, bmc_opts, fake_pyghmi):
    """Regression: BMCs that reject >16-byte FRU reads with 0xC7 still produce a result."""
    bmc_opts["pillar"]["saltext.bmc"]["profiles"]["test-host"]["backend"] = "ipmi"
    monkeypatch.setattr(bmc_mod, "__opts__", bmc_opts, raising=False)

    def _serve_with_limit(*, netfn, command, data):
        if netfn == 0x0A and command == 0x11 and data[3] > 16:
            return {
                "code": 0xC7,
                "data": bytearray(),
                "error": "Request data length invalid",
            }
        return _serve_fru(netfn=netfn, command=command, data=data)

    instance = fake_pyghmi.return_value
    instance.raw_command.side_effect = _serve_with_limit
    instance.get_firmware.return_value = iter([("BMC Version", {"version": "1.26"})])
    instance.get_power.return_value = {"powerstate": "off"}

    info = bmc_mod.get_system_info("test-host")
    assert info["manufacturer"] == "Supermicro"
    assert info["model"] == "X9DR3-LN4F+"
    assert info["serial_number"] == "S12345"
    assert info["firmware_version"] == "1.26"
    assert info["power_state"] == "off"


# ---------------------------------------------------------------------------
# get_sensor_data — Redfish
# ---------------------------------------------------------------------------


def test_redfish_sensor_data(mocked_redfish_full):
    mocked_redfish_full.replace(
        responses.GET,
        f"{REDFISH_BASE}{REDFISH_SYS_PATH}",
        json={
            "@odata.id": REDFISH_SYS_PATH,
            "PowerState": "On",
            "Boot": {},
            "Actions": {"#ComputerSystem.Reset": {"target": ""}},
            "Links": {"Chassis": [{"@odata.id": "/redfish/v1/Chassis/1"}]},
        },
    )
    mocked_redfish_full.add(
        responses.GET,
        f"{REDFISH_BASE}/redfish/v1/Chassis/1/Thermal",
        json={
            "Temperatures": [
                {
                    "Name": "CPU 1",
                    "ReadingCelsius": 38,
                    "Status": {"Health": "OK"},
                },
                {
                    "Name": "Inlet",
                    "ReadingCelsius": 22,
                    "Status": {"Health": "OK"},
                },
            ],
            "Fans": [
                {
                    "Name": "Fan1",
                    "Reading": 4200,
                    "ReadingUnits": "RPM",
                    "Status": {"Health": "OK"},
                },
            ],
        },
    )
    mocked_redfish_full.add(
        responses.GET,
        f"{REDFISH_BASE}/redfish/v1/Chassis/1/Power",
        json={
            "Voltages": [
                {
                    "Name": "VDD_CPU",
                    "ReadingVolts": 1.05,
                    "Status": {"Health": "OK"},
                },
            ],
        },
    )

    data = bmc_mod.get_sensor_data("test-host")
    assert [t["name"] for t in data["temperatures"]] == ["CPU 1", "Inlet"]
    assert data["temperatures"][0]["reading"] == 38
    assert data["temperatures"][0]["unit"] == "C"
    assert data["fans"][0]["name"] == "Fan1"
    assert data["fans"][0]["reading"] == 4200
    assert data["voltages"][0]["reading"] == 1.05


def test_redfish_sensor_data_no_chassis_returns_empty(mocked_redfish_full):
    # System has no Links.Chassis; /redfish/v1/Chassis is empty too.
    mocked_redfish_full.add(
        responses.GET,
        f"{REDFISH_BASE}/redfish/v1/Chassis",
        json={"Members": []},
    )
    data = bmc_mod.get_sensor_data("test-host")
    assert data == {"temperatures": [], "fans": [], "voltages": []}


# ---------------------------------------------------------------------------
# get_sensor_data — IPMI
# ---------------------------------------------------------------------------


def test_ipmi_sensor_data(monkeypatch, bmc_opts, fake_pyghmi):
    bmc_opts["pillar"]["saltext.bmc"]["profiles"]["test-host"]["backend"] = "ipmi"
    monkeypatch.setattr(bmc_mod, "__opts__", bmc_opts, raising=False)

    def reading(name, value, units, health="ok"):
        # pyghmi SensorReading is attribute-style; emulate with a dict here —
        # the IpmiClient helper accepts both.
        return {"name": name, "value": value, "units": units, "health": health}

    instance = fake_pyghmi.return_value
    instance.get_sensor_data.return_value = [
        reading("CPU Temp", 42.0, "Degrees C"),
        reading("Fan1", 5100, "RPM"),
        reading("VCC", 3.31, "Volts"),
        reading("Some Counter", 17, "Count"),  # should be discarded
    ]

    data = bmc_mod.get_sensor_data("test-host")
    assert [t["name"] for t in data["temperatures"]] == ["CPU Temp"]
    assert data["temperatures"][0]["reading"] == 42.0
    assert data["temperatures"][0]["unit"] == "C"
    assert data["fans"][0]["name"] == "Fan1"
    assert data["voltages"][0]["name"] == "VCC"
    # The unrecognised counter is dropped entirely.


# ---------------------------------------------------------------------------
# bmc_redfish passthrough
# ---------------------------------------------------------------------------


def test_bmc_redfish_get(mocked_redfish):
    mocked_redfish.add(
        responses.GET,
        f"{REDFISH_BASE}/redfish/v1/",
        json={"Name": "Service Root", "RedfishVersion": "1.16.0"},
    )
    result = bmc_redfish.get("/redfish/v1/", name="test-host")
    assert result["RedfishVersion"] == "1.16.0"


def test_bmc_redfish_patch_includes_body(mocked_redfish):
    mocked_redfish.add(
        responses.PATCH,
        f"{REDFISH_BASE}{REDFISH_SYS_PATH}",
        json={"AssetTag": "rack-7-slot-3"},
        status=200,
    )
    result = bmc_redfish.patch(REDFISH_SYS_PATH, {"AssetTag": "rack-7-slot-3"}, name="test-host")
    assert result["AssetTag"] == "rack-7-slot-3"
    sent = [c.request for c in mocked_redfish.calls if c.request.method == "PATCH"][0]
    assert json.loads(sent.body) == {"AssetTag": "rack-7-slot-3"}


def test_bmc_redfish_delete(mocked_redfish):
    mocked_redfish.add(
        responses.DELETE,
        f"{REDFISH_BASE}/redfish/v1/SessionService/Sessions/2",
        status=204,
    )
    result = bmc_redfish.delete("/redfish/v1/SessionService/Sessions/2", name="test-host")
    assert result is None  # empty body


def test_bmc_redfish_rejects_ipmi_profile(monkeypatch, bmc_opts):
    bmc_opts["pillar"]["saltext.bmc"]["profiles"]["test-host"]["backend"] = "ipmi"
    monkeypatch.setattr(bmc_redfish, "__opts__", bmc_opts, raising=False)
    with pytest.raises(rf.RedfishError):
        bmc_redfish.get("/redfish/v1/", name="test-host")
