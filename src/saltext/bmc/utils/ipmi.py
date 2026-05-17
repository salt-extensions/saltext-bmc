"""
IPMI transport for the ``saltext-bmc`` extension.

Thin wrapper over ``pyghmi.ipmi.command.Command``.  ``pyghmi`` is an
optional dependency — install with ``pip install saltext.bmc[ipmi]``.
The module imports it lazily so the extension still loads in
Redfish-only environments.

This module exposes a uniform interface that mirrors the Redfish one in
:mod:`saltext.bmc.utils.redfish`, taking canonical action / boot-device
names and translating them to IPMI semantics:

============================  ============================
Canonical action               IPMI translation
============================  ============================
``On``                         ``set_power('on')``
``ForceOff``                   ``set_power('off')``
``GracefulShutdown``           ``set_power('shutdown')``
``PowerCycle``                 IPMI chassis control 0x02
``GracefulRestart``            ``set_power('reset')``
``ForceRestart``               ``set_power('reset')``
============================  ============================

============================  ============================
Canonical boot device          pyghmi ``bootdev``
============================  ============================
``disk``                       ``hd``
``pxe``                        ``network``
``bios``                       ``setup``
``cd``                         ``cd``
``none``                       ``default``
``http`` / ``usb``              **not supported** (raises)
============================  ============================
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30
DEFAULT_PORT = 623

# Canonical reset names → pyghmi set_power() values.
_RESET_TO_PYGHMI = {
    "On": "on",
    "ForceOff": "off",
    "GracefulShutdown": "shutdown",
    "GracefulRestart": "reset",
    "ForceRestart": "reset",
    # PowerCycle is handled via raw IPMI chassis control (see _do_cycle).
}

# Canonical boot device → pyghmi bootdev value.
_DEVICE_TO_PYGHMI = {
    "disk": "hd",
    "pxe": "network",
    "bios": "setup",
    "cd": "cd",
    "none": "default",
}
_DEVICE_FROM_PYGHMI = {v: k for k, v in _DEVICE_TO_PYGHMI.items()}
_DEVICE_FROM_PYGHMI.update(
    {
        "safe": "disk",  # boot to disk in safe mode → treat as disk
        "diag": "bios",  # diagnostic partition → bios is closest friendly
        "floppy": "cd",  # legacy
    }
)


class IpmiError(Exception):
    """An IPMI operation failed."""


class IpmiAuthError(IpmiError):
    """Authentication against the IPMI endpoint failed."""


# Candidate key names by normalized output field, in priority order.
# pyghmi's FRU info dict (from get_inventory_of_component) uses title-case
# keys with spaces ('Product name', 'Serial Number', 'Manufacturer', ...).
# Older callers / test fixtures use snake_case ('product_name', ...).
_FRU_KEY_ALIASES = {
    "manufacturer": ["Manufacturer", "product_manufacturer", "manufacturer"],
    "model": ["Product name", "Model", "product_name", "model", "name"],
    "serial_number": ["Serial Number", "product_serial", "serial_number"],
    "uuid": ["UUID", "uuid"],
    "sku": ["SKU", "product_sku", "sku"],
}


def _pick(src: dict, candidates: list[str]):
    """First non-empty value from ``src`` for any of ``candidates`` (case-insensitive fallback)."""
    for key in candidates:
        value = src.get(key)
        if value:
            return value
    lower = {str(k).lower(): v for k, v in src.items()}
    for key in candidates:
        value = lower.get(key.lower())
        if value:
            return value
    return None


def _select_fru_product(fru) -> dict:
    """
    Return normalized inventory fields from a pyghmi FRU dict.

    pyghmi's :meth:`get_inventory_of_component` returns a flat dict with
    title-case keys.  Older / nested shapes are still accepted for
    backwards compatibility with callers feeding us pre-normalized data.
    """
    if not isinstance(fru, dict) or not fru:
        return {}
    # Nested 'product' shape (legacy)
    if isinstance(fru.get("product"), dict):
        return _normalize_fru_nested(fru)
    # Dict-of-FRUs keyed by id (legacy) — pick the most informative entry.
    if not any(k in fru for k in ("Product name", "product_name", "Manufacturer", "manufacturer")):
        for entry in fru.values():
            if isinstance(entry, dict):
                got = _select_fru_product(entry)
                if got:
                    return got
        return {}
    return _normalize_fru_flat(fru)


def _normalize_fru_flat(fru: dict) -> dict:
    return {
        "manufacturer": _pick(fru, _FRU_KEY_ALIASES["manufacturer"]),
        "model": _pick(fru, _FRU_KEY_ALIASES["model"]),
        "serial_number": _pick(fru, _FRU_KEY_ALIASES["serial_number"]),
        "uuid": _pick(fru, _FRU_KEY_ALIASES["uuid"]),
        "sku": _pick(fru, _FRU_KEY_ALIASES["sku"]),
    }


def _normalize_fru_nested(fru: dict) -> dict:
    product = fru.get("product") or {}
    return {
        "manufacturer": _pick(product, _FRU_KEY_ALIASES["manufacturer"]),
        "model": _pick(product, _FRU_KEY_ALIASES["model"]),
        "serial_number": _pick(product, _FRU_KEY_ALIASES["serial_number"]),
        "uuid": _pick(fru, _FRU_KEY_ALIASES["uuid"]) or _pick(product, _FRU_KEY_ALIASES["uuid"]),
        "sku": _pick(product, _FRU_KEY_ALIASES["sku"]),
    }


def _attr(obj, name, default=None):
    """Get attribute from object or key from dict."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _load_command_cls():
    """Lazy-load ``pyghmi.ipmi.command.Command``.

    Raises a clear :class:`IpmiError` if ``pyghmi`` is not installed.
    """
    try:
        from pyghmi.ipmi import command as _command  # pylint: disable=import-outside-toplevel
    except ImportError as exc:  # pragma: no cover - exercised via dependency mocking
        raise IpmiError(
            "pyghmi is required for the IPMI backend. "
            "Install with: pip install 'saltext.bmc[ipmi]'"
        ) from exc
    return _command.Command


def _load_fru_cls():
    """Lazy-load ``pyghmi.ipmi.fru.FRU`` for raw-bytes parsing."""
    try:
        from pyghmi.ipmi import fru as _fru  # pylint: disable=import-outside-toplevel
    except ImportError as exc:  # pragma: no cover
        raise IpmiError("pyghmi is required for the IPMI backend.") from exc
    return _fru.FRU


# IPMI completion codes that signal "chunk size too large".  pyghmi's
# fetch_fru only retries on 0xC9 / 0xCA, which leaves older BMCs that
# return 0xC7 ("Request data length invalid") for >16-byte reads broken.
# See pyghmi/ipmi/fru.py fetch_fru().
_FRU_CHUNK_TOO_BIG_CODES = (0xC7, 0xC9, 0xCA)

# IPMI completion codes that mean "no FRU device" (treat as empty).
_FRU_ABSENT_CODES = (0xC1, 0xC3, 0xCB)


def _fetch_system_fru(cmd, fruid: int = 0) -> dict:
    """
    Read FRU data via raw IPMI commands and parse via ``pyghmi.ipmi.fru.FRU``.

    Replaces pyghmi's :meth:`Command.get_inventory_of_component` for the
    system FRU.  Pyghmi's built-in reader starts at a 224-byte chunk size
    and only backs off on completion codes ``0xC9``/``0xCA``; some legacy
    BMCs return ``0xC7`` instead, which propagates as an unhandled
    exception.  This implementation backs off on all three codes, so it
    works on both modern hardware (succeeds at 224 bytes) and older
    hardware that needs 16 bytes or less.

    Returns the parsed FRU info dict (the same shape pyghmi would return),
    or ``{}`` if the BMC does not have FRU ``fruid``.
    """
    info_resp = cmd.raw_command(netfn=0x0A, command=0x10, data=[fruid])
    code = info_resp.get("code", 0)
    if code in _FRU_ABSENT_CODES:
        return {}
    if code != 0:
        raise IpmiError(
            f"FRU inventory area read failed (code {code:#x}): "
            f"{info_resp.get('error', 'unknown')}"
        )
    data = info_resp["data"]
    frusize = data[0] | (data[1] << 8)
    if frusize == 0:
        return {}

    rawfru = bytearray()
    chunksize = 224  # pyghmi's tested ceiling; we back off on errors
    offset = 0
    while offset < frusize:
        want = min(chunksize, frusize - offset)
        resp = cmd.raw_command(
            netfn=0x0A,
            command=0x11,
            data=[fruid, offset & 0xFF, (offset >> 8) & 0xFF, want],
        )
        code = resp.get("code", 0)
        if code in _FRU_CHUNK_TOO_BIG_CODES:
            if chunksize <= 3:
                raise IpmiError(
                    f"FRU read failed at offset {offset}: BMC rejects even minimal chunk size"
                )
            # Same back-off formula pyghmi uses: roughly halve, plus 2 to
            # avoid the chunksize=3 lower bound on the next iteration.
            chunksize = max(3, chunksize // 2 + 2)
            continue
        if code != 0:
            raise IpmiError(
                f"FRU read failed at offset {offset} (code {code:#x}): "
                f"{resp.get('error', 'unknown')}"
            )
        returned = resp["data"][0]
        if returned == 0:
            break
        rawfru.extend(resp["data"][1 : 1 + returned])
        offset += returned

    fru_cls = _load_fru_cls()
    return fru_cls(rawdata=rawfru).info or {}


def _wrap_pyghmi_errors(method):
    """Re-raise pyghmi exceptions as IpmiError / IpmiAuthError."""

    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except IpmiError:
            raise
        except Exception as exc:  # pylint: disable=broad-except
            # pyghmi raises various IpmiException subclasses + socket errors.
            msg = str(exc).lower()
            if "unauthorized" in msg or "permission" in msg or "credentials" in msg:
                raise IpmiAuthError(f"IPMI auth failed: {exc}") from exc
            raise IpmiError(f"IPMI operation failed: {exc}") from exc

    return wrapper


class IpmiClient:
    """
    Thin IPMI client wrapping :class:`pyghmi.ipmi.command.Command`.

    Use as a context manager::

        with IpmiClient(host, user, password) as c:
            state = c.power_status()
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        port: int = DEFAULT_PORT,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.timeout = timeout
        self._cmd = None

    def __enter__(self) -> IpmiClient:
        cmd_cls = _load_command_cls()
        try:
            self._cmd = cmd_cls(
                bmc=self.host,
                userid=self.username,
                password=self.password,
                port=self.port,
            )
        except Exception as exc:  # pylint: disable=broad-except
            msg = str(exc).lower()
            if "unauthorized" in msg or "permission" in msg or "credentials" in msg:
                raise IpmiAuthError(f"IPMI session to {self.host} rejected: {exc}") from exc
            raise IpmiError(f"Cannot reach IPMI endpoint at {self.host}: {exc}") from exc
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # pyghmi.Command holds an internal Session; explicit logout is
        # exposed as `ipmi_session.logout()`, but pyghmi cleans up on GC.
        # We drop our reference so the session can close.
        self._cmd = None

    # ------------------------------------------------------------------
    # Power
    # ------------------------------------------------------------------

    @_wrap_pyghmi_errors
    def power_status(self) -> str:
        result = self._cmd.get_power()
        state = (result or {}).get("powerstate", "")
        s = state.lower()
        return s if s in ("on", "off") else "unknown"

    @_wrap_pyghmi_errors
    def do_reset(self, reset_type: str) -> dict:
        if reset_type == "PowerCycle":
            return self._do_cycle()
        native = _RESET_TO_PYGHMI.get(reset_type)
        if native is None:
            raise IpmiError(f"Unknown reset type {reset_type!r} for IPMI backend.")
        self._cmd.set_power(native)
        return {"action": reset_type, "result": True}

    def _do_cycle(self) -> dict:
        """Issue an IPMI Chassis Control 0x02 (Power Cycle)."""
        # netfn 0x00 (chassis), command 0x02 (chassis control), data 0x02 (power cycle)
        self._cmd.raw_command(netfn=0x00, command=0x02, data=[0x02])
        return {"action": "PowerCycle", "result": True}

    # ------------------------------------------------------------------
    # Boot device
    # ------------------------------------------------------------------

    @_wrap_pyghmi_errors
    def get_boot(self) -> dict:
        raw = self._cmd.get_bootdev() or {}
        native = raw.get("bootdev", "default")
        friendly = _DEVICE_FROM_PYGHMI.get(native, "none")
        persistent = bool(raw.get("persistent", False))
        if native == "default":
            enabled = "Disabled"
        else:
            enabled = "Continuous" if persistent else "Once"
        return {
            "device": friendly,
            "redfish_target": None,  # not applicable
            "native_target": native,
            "enabled": enabled,
        }

    @_wrap_pyghmi_errors
    def get_system_info(self) -> dict:
        """
        Return a normalised inventory dict (some fields may be ``None``).

        Reads ``get_inventory_of_component('System')`` for FRU-derived
        product info, ``get_firmware()`` for the BMC firmware version, and
        ``get_power()`` for ``power_state``.  Each piece is best-effort —
        BMCs with broken or absent FRU data (common on older hardware) will
        return ``None`` for the affected fields rather than failing the
        whole call.
        """
        # We read the FRU ourselves rather than calling pyghmi's
        # get_inventory_of_component('System'), which is broken on legacy
        # BMCs that return IPMI completion code 0xC7 for >16-byte chunks.
        try:
            sys_fru = _fetch_system_fru(self._cmd)
        except Exception:  # pylint: disable=broad-except
            sys_fru = {}
        product = _select_fru_product(sys_fru)

        # get_firmware() yields (component_name, {'version': str, ...}) tuples.
        # The generic OEM handler always yields a 'BMC Version' entry; vendor
        # OEMs may yield BIOS and component firmware too.
        fw_version = None
        try:
            for name, info in self._cmd.get_firmware() or []:
                if name and "bmc" in name.lower():
                    fw_version = (info or {}).get("version")
                    break
        except Exception:  # pylint: disable=broad-except
            pass

        try:
            power_raw = (self._cmd.get_power() or {}).get("powerstate", "")
        except Exception:  # pylint: disable=broad-except
            power_raw = ""
        power = power_raw.lower() if power_raw.lower() in ("on", "off") else None

        return {
            "manufacturer": product.get("manufacturer"),
            "model": product.get("model"),
            "serial_number": product.get("serial_number"),
            "uuid": product.get("uuid"),
            "sku": product.get("sku"),
            "host_name": None,  # IPMI does not expose this directly
            "bios_version": None,  # IPMI does not expose this directly
            "firmware_version": fw_version,
            "power_state": power,
        }

    @_wrap_pyghmi_errors
    def get_sensors(self) -> dict:
        """
        Return ``{"temperatures": [...], "fans": [...], "voltages": [...]}``
        bucketed by pyghmi's unit hint.
        """
        temperatures: list[dict] = []
        fans: list[dict] = []
        voltages: list[dict] = []
        for reading in self._cmd.get_sensor_data() or []:
            unit = _attr(reading, "units", default="") or ""
            entry = {
                "name": _attr(reading, "name", default=None),
                "reading": _attr(reading, "value", default=None),
                "unit": unit,
                "status": _attr(reading, "health", default=None)
                or _attr(reading, "state", default=None),
            }
            u = unit.lower()
            if "celsius" in u or u in ("c", "°c", "degrees c"):
                entry["unit"] = "C"
                temperatures.append(entry)
            elif "rpm" in u:
                entry["unit"] = "RPM"
                fans.append(entry)
            elif "volt" in u or u == "v":
                entry["unit"] = "V"
                voltages.append(entry)
        return {"temperatures": temperatures, "fans": fans, "voltages": voltages}

    @_wrap_pyghmi_errors
    def set_boot(self, device: str, persistent: bool = False) -> dict:
        native = _DEVICE_TO_PYGHMI.get(device)
        if native is None:
            raise IpmiError(
                f"Boot device {device!r} is not supported over IPMI. "
                "Use the Redfish backend, or pick one of: "
                f"{', '.join(sorted(_DEVICE_TO_PYGHMI))}."
            )
        # uefiboot is best-effort; most modern systems accept it.
        self._cmd.set_bootdev(native, persist=persistent, uefiboot=False)
        enabled = "Continuous" if persistent else "Once"
        if native == "default":
            enabled = "Disabled"
        return {
            "device": device,
            "redfish_target": None,
            "native_target": native,
            "enabled": enabled,
            "result": True,
        }
