"""
Protocol-agnostic BMC backend dispatcher.

The public surface (``power_status``, ``do_reset``, ``get_boot``,
``set_boot``) is identical regardless of whether the underlying
transport is Redfish or IPMI.  Modules and states call
:func:`open_backend`, get a backend instance, and stay protocol-neutral.

Backend selection (from pillar)::

    saltext.bmc:
      profiles:
        bmc-host-01:
          host: 10.0.0.5
          username: root
          password: calvin
          verify_ssl: false
          backend: redfish   # or 'ipmi', or 'auto' (default)

* ``auto``    (default) — probe Redfish first; fall back to IPMI on TLS
  handshake failure, connection error, or any non-Redfish HTTP response
  from ``/redfish/v1/``.  Handles mixed-hardware fleets out of the box.
* ``redfish``           — Redfish only (skips the probe).
* ``ipmi``              — IPMI only (requires ``pyghmi``).
"""

from __future__ import annotations

import logging
from abc import ABC
from abc import abstractmethod
from typing import Any

import requests
import urllib3

from saltext.bmc.utils import boot as boot_canon
from saltext.bmc.utils import ipmi as ipmi_util
from saltext.bmc.utils import redfish as rf

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Canonical Redfish boot-device mapping
# ----------------------------------------------------------------------

_REDFISH_DEVICE = {
    "disk": "Hdd",
    "pxe": "Pxe",
    "http": "UefiHttp",
    "bios": "BiosSetup",
    "cd": "Cd",
    "usb": "Usb",
    "none": "None",
}
_DEVICE_FROM_REDFISH = {v: k for k, v in _REDFISH_DEVICE.items()}


# ----------------------------------------------------------------------
# Abstract backend
# ----------------------------------------------------------------------


class BmcBackend(ABC):
    """Common interface implemented by Redfish and IPMI backends."""

    name: str = "abstract"

    @abstractmethod
    def open(self) -> None:
        """Establish the underlying session."""

    @abstractmethod
    def close(self) -> None:
        """Tear down the underlying session."""

    @abstractmethod
    def power_status(self) -> str:
        """Return ``'on'``, ``'off'``, or ``'unknown'``."""

    @abstractmethod
    def do_reset(self, reset_type: str) -> dict:
        """
        Issue a canonical reset action.

        :param reset_type: one of ``On``, ``ForceOff``, ``GracefulShutdown``,
            ``PowerCycle``, ``GracefulRestart``, ``ForceRestart``.
        """

    @abstractmethod
    def get_boot(self) -> dict:
        """Return a dict with ``device`` (friendly), ``enabled``, ``native_target``."""

    @abstractmethod
    def set_boot(self, device: str, persistent: bool = False) -> dict:
        """Set the boot override and return a normalized dict."""

    @abstractmethod
    def get_system_info(self) -> dict:
        """
        Return a normalized inventory dict with keys:
        ``manufacturer``, ``model``, ``serial_number``, ``uuid``, ``sku``,
        ``host_name``, ``bios_version``, ``firmware_version``, ``power_state``.
        Any unknown field is ``None``.
        """

    @abstractmethod
    def get_sensors(self) -> dict:
        """
        Return a dict ``{"temperatures": [...], "fans": [...], "voltages": [...]}``
        where each list item has ``name``, ``reading``, ``unit``, ``status``.
        Empty lists are returned for sensor classes the backend does not expose.
        """

    # Context-manager wrapping over the above primitives.
    def __enter__(self) -> BmcBackend:
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


# ----------------------------------------------------------------------
# Redfish backend
# ----------------------------------------------------------------------


class RedfishBackend(BmcBackend):
    name = "redfish"

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        verify_ssl: bool = True,
        timeout: int = rf.DEFAULT_TIMEOUT,
    ) -> None:
        self._client = rf.RedfishClient(
            host=host,
            username=username,
            password=password,
            verify_ssl=verify_ssl,
            timeout=timeout,
        )
        self._sys_path: str | None = None

    def open(self) -> None:
        self._client.__enter__()  # pylint: disable=unnecessary-dunder-call
        self._sys_path = rf.get_system_path(self._client)

    def close(self) -> None:
        self._client.__exit__(None, None, None)

    def _system(self) -> dict:
        assert self._sys_path is not None
        return self._client.get(self._sys_path)

    def power_status(self) -> str:
        raw = (self._system().get("PowerState") or "").lower()
        return raw if raw in ("on", "off") else "unknown"

    def do_reset(self, reset_type: str) -> dict:
        system = self._system()
        target, _allowable = rf.get_reset_action(system)
        self._client.post(target, {"ResetType": reset_type})
        return {"action": reset_type, "result": True}

    def get_boot(self) -> dict:
        boot = self._system().get("Boot", {}) or {}
        target = boot.get("BootSourceOverrideTarget", "None")
        return {
            "device": _DEVICE_FROM_REDFISH.get(target, "none"),
            "redfish_target": target,
            "native_target": target,
            "enabled": boot.get("BootSourceOverrideEnabled", "Disabled"),
        }

    def set_boot(self, device: str, persistent: bool = False) -> dict:
        canon = boot_canon.canonicalize(device)
        target = _REDFISH_DEVICE.get(canon)
        if target is None:
            raise rf.RedfishError(f"No Redfish target for canonical device {canon!r}")
        enabled = "Continuous" if persistent else "Once"
        assert self._sys_path is not None
        self._client.patch(
            self._sys_path,
            {
                "Boot": {
                    "BootSourceOverrideEnabled": enabled,
                    "BootSourceOverrideTarget": target,
                }
            },
        )
        return {
            "device": canon,
            "redfish_target": target,
            "native_target": target,
            "enabled": enabled,
            "result": True,
        }

    def get_system_info(self) -> dict:
        system = self._system()
        fw_version = self._manager_firmware_version()
        return {
            "manufacturer": system.get("Manufacturer"),
            "model": system.get("Model"),
            "serial_number": system.get("SerialNumber"),
            "uuid": system.get("UUID"),
            "sku": system.get("SKU"),
            "host_name": system.get("HostName"),
            "bios_version": system.get("BiosVersion"),
            "firmware_version": fw_version,
            "power_state": (system.get("PowerState") or "").lower() or None,
        }

    def _manager_firmware_version(self) -> str | None:
        """Best-effort fetch of the BMC firmware version from /redfish/v1/Managers."""
        try:
            coll = self._client.get("/redfish/v1/Managers")
        except rf.RedfishError:
            return None
        members = coll.get("Members") or []
        if not members:
            return None
        try:
            mgr = self._client.get(members[0]["@odata.id"])
        except rf.RedfishError:
            return None
        return mgr.get("FirmwareVersion")

    def get_sensors(self) -> dict:
        chassis_path = self._chassis_path()
        temperatures: list[dict] = []
        fans: list[dict] = []
        voltages: list[dict] = []
        if chassis_path is None:
            return {"temperatures": temperatures, "fans": fans, "voltages": voltages}
        # Thermal
        try:
            thermal = self._client.get(f"{chassis_path}/Thermal")
        except rf.RedfishError:
            thermal = {}
        for entry in thermal.get("Temperatures") or []:
            temperatures.append(
                {
                    "name": entry.get("Name"),
                    "reading": entry.get("ReadingCelsius"),
                    "unit": "C",
                    "status": (entry.get("Status") or {}).get("Health"),
                }
            )
        for entry in thermal.get("Fans") or []:
            fans.append(
                {
                    "name": entry.get("Name") or entry.get("FanName"),
                    "reading": entry.get("Reading"),
                    "unit": entry.get("ReadingUnits") or "RPM",
                    "status": (entry.get("Status") or {}).get("Health"),
                }
            )
        # Power
        try:
            power = self._client.get(f"{chassis_path}/Power")
        except rf.RedfishError:
            power = {}
        for entry in power.get("Voltages") or []:
            voltages.append(
                {
                    "name": entry.get("Name"),
                    "reading": entry.get("ReadingVolts"),
                    "unit": "V",
                    "status": (entry.get("Status") or {}).get("Health"),
                }
            )
        return {"temperatures": temperatures, "fans": fans, "voltages": voltages}

    def _chassis_path(self) -> str | None:
        """Pick the chassis associated with this system (or the first available)."""
        system = self._system()
        links = system.get("Links", {}) or {}
        chassis_links = links.get("Chassis") or []
        for entry in chassis_links:
            path = entry.get("@odata.id") if isinstance(entry, dict) else None
            if path:
                return path
        try:
            coll = self._client.get("/redfish/v1/Chassis")
        except rf.RedfishError:
            return None
        members = coll.get("Members") or []
        if not members:
            return None
        return members[0].get("@odata.id")


# ----------------------------------------------------------------------
# IPMI backend
# ----------------------------------------------------------------------


class IpmiBackend(BmcBackend):
    name = "ipmi"

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        port: int = ipmi_util.DEFAULT_PORT,
        timeout: int = ipmi_util.DEFAULT_TIMEOUT,
        **_unused,
    ) -> None:
        self._client = ipmi_util.IpmiClient(
            host=host,
            username=username,
            password=password,
            port=port,
            timeout=timeout,
        )

    def open(self) -> None:
        self._client.__enter__()  # pylint: disable=unnecessary-dunder-call

    def close(self) -> None:
        self._client.__exit__(None, None, None)

    def power_status(self) -> str:
        return self._client.power_status()

    def do_reset(self, reset_type: str) -> dict:
        return self._client.do_reset(reset_type)

    def get_boot(self) -> dict:
        return self._client.get_boot()

    def set_boot(self, device: str, persistent: bool = False) -> dict:
        canon = boot_canon.canonicalize(device)
        return self._client.set_boot(canon, persistent=persistent)

    def get_system_info(self) -> dict:
        return self._client.get_system_info()

    def get_sensors(self) -> dict:
        return self._client.get_sensors()


# ----------------------------------------------------------------------
# Selection / factory
# ----------------------------------------------------------------------


def _probe_redfish(host: str, verify_ssl: bool, timeout: int) -> bool:
    """Return True if ``https://<host>/redfish/v1/`` looks like a Redfish service root."""
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    try:
        resp = requests.get(
            f"https://{host}/redfish/v1/",
            verify=verify_ssl,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        log.debug("Redfish probe failed at %s: %s", host, exc)
        return False
    if resp.status_code in (200, 401, 403):
        # 401/403 still means there's a Redfish service responding.
        return True
    return False


def _build_backend(kind: str, cfg: dict) -> BmcBackend:
    if kind == "redfish":
        return RedfishBackend(
            host=cfg["host"],
            username=cfg["username"],
            password=cfg["password"],
            verify_ssl=cfg.get("verify_ssl", True),
            timeout=cfg.get("timeout", rf.DEFAULT_TIMEOUT),
        )
    if kind == "ipmi":
        return IpmiBackend(
            host=cfg["host"],
            username=cfg["username"],
            password=cfg["password"],
            port=cfg.get("port", ipmi_util.DEFAULT_PORT),
            timeout=cfg.get("timeout", ipmi_util.DEFAULT_TIMEOUT),
        )
    raise ValueError(f"Unknown backend {kind!r}; expected redfish, ipmi, or auto.")


def _resolve_backend_kind(cfg: dict, overrides: dict) -> str:
    # Default to 'auto' so mixed-hardware fleets work out of the box without
    # forcing every profile to declare a backend.  Users who only have one
    # protocol can still pin explicitly to avoid the per-call probe overhead.
    requested = (overrides.get("backend") or cfg.get("backend") or "auto").lower()
    if requested not in ("redfish", "ipmi", "auto"):
        raise ValueError(f"Unknown backend {requested!r}; expected redfish, ipmi, or auto.")
    if requested != "auto":
        return requested
    # auto-probe
    if _probe_redfish(
        cfg["host"],
        verify_ssl=cfg.get("verify_ssl", True),
        timeout=cfg.get("timeout", rf.DEFAULT_TIMEOUT),
    ):
        log.debug("Auto backend selected redfish for %s", cfg["host"])
        return "redfish"
    log.debug("Auto backend selected ipmi for %s (Redfish probe failed)", cfg["host"])
    return "ipmi"


def open_backend(opts: dict, name: str | None = None, **overrides: Any) -> BmcBackend:
    """
    Build a context-managed :class:`BmcBackend` from opts/pillar + overrides.

    The returned object MUST be used as a context manager::

        with open_backend(__opts__, name="bmc-host-01") as backend:
            backend.power_status()
    """
    # The redfish resolver propagates backend/port through transparently — its
    # name is historical, the schema is protocol-agnostic.
    cfg = rf.resolve_conn(opts, name=name, **overrides)
    if cfg.get("backend") is None:
        # Honour a 'backend' key under the top-level saltext.bmc settings as a fallback.
        pillar_root = (
            opts.get("pillar", {}).get("saltext.bmc", {}) if isinstance(opts, dict) else {}
        )
        cfg["backend"] = pillar_root.get("backend")
    kind = _resolve_backend_kind(cfg, overrides)
    return _build_backend(kind, cfg)
