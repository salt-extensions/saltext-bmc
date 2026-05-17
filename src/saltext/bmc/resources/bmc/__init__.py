"""
``bmc`` resource type — per-host BMC management over Redfish or IPMI.

Each ``bmc`` resource maps to one physical machine, addressed by its BMC
host/credentials.  The resource ID is the human-friendly name used in
Pillar and targeting.

Configuration (via Pillar)::

    resources:
      bmc:
        bmc-host-01:
          host: 10.10.10.5
          username: root
          password: calvin
          verify_ssl: false
          # backend defaults to 'auto'; set explicitly to skip the probe:
          # backend: redfish        # or 'ipmi'
        legacy-host:
          host: 10.10.10.7
          username: ADMIN
          password: ADMIN
          backend: ipmi
          port: 623

Targeting::

    salt  bmc-host-01              bmc_host.power_status   # by resource ID alone
    salt -C 'T@bmc:bmc-host-01'    bmc_host.power_status   # by full SRN
    salt -C 'T@bmc'                bmc_host.power_status   # all bmc resources
    salt  bmc-host-01              state.sls baremetal/boot_http

Per-host execution functions live in
:mod:`saltext.bmc.resources.bmc.modules.bmc_host`; the per-host states
in :mod:`saltext.bmc.resources.bmc.states.bmc_host`.
"""

from __future__ import annotations

import logging

import salt.utils.resources  # pylint: disable=import-error,no-name-in-module

from saltext.bmc.utils import backend as bk

log = logging.getLogger(__name__)

CONTEXT_KEY = "bmc_resource"


def __virtual__():
    return True


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resource_id() -> str:
    return __resource__["id"]  # pylint: disable=undefined-variable


def _ctx() -> dict:
    return __context__.get(CONTEXT_KEY, {})  # pylint: disable=undefined-variable


def _host_cfg(resource_id: str) -> dict:
    cfg = _ctx().get("hosts", {}).get(resource_id, {})
    if not cfg:
        raise ValueError(f"No bmc resource '{resource_id}' in pillar resources:bmc:")
    return cfg


def _opts_for(resource_id: str) -> dict:
    """
    Build a Salt-opts shim that routes through :func:`backend.open_backend`.

    ``open_backend`` reads its connection config from ``opts["pillar"]["saltext.bmc"]``;
    we synthesise that from the per-resource pillar entry so we can reuse the same
    factory.
    """
    cfg = _host_cfg(resource_id)
    return {
        "pillar": {
            "saltext.bmc": {
                "profiles": {resource_id: cfg},
            }
        }
    }


def _open(resource_id: str | None = None):
    rid = resource_id or _resource_id()
    return bk.open_backend(_opts_for(rid), name=rid)


# ---------------------------------------------------------------------------
# Required resource interface
# ---------------------------------------------------------------------------


def init(opts: dict) -> None:
    """
    Initialise the ``bmc`` resource type.

    Reads BMC host configs from Pillar and caches them in
    ``__context__["bmc_resource"]``.
    """
    type_cfg = salt.utils.resources.pillar_resources_tree(opts).get("bmc", {}) or {}
    __context__[CONTEXT_KEY] = {  # pylint: disable=undefined-variable
        "initialized": True,
        "hosts": type_cfg,
    }
    log.debug("bmc init(), managing: %s", list(type_cfg))


def initialized() -> bool:
    """Return ``True`` if :func:`init` has been called successfully."""
    return _ctx().get("initialized", False)


def discover(opts: dict) -> list:
    """
    Return the list of BMC resource IDs declared in Pillar.

    :param dict opts: The Salt opts dict.
    :rtype: list[str]
    """
    type_cfg = salt.utils.resources.pillar_resources_tree(opts).get("bmc", {}) or {}
    return list(type_cfg.keys())


# ---------------------------------------------------------------------------
# Per-resource operations
# ---------------------------------------------------------------------------


def power_status() -> str:
    """Return ``'on'`` / ``'off'`` / ``'unknown'`` for this host."""
    with _open() as backend:
        return backend.power_status()


def power_on() -> dict:
    """Power on this host."""
    with _open() as backend:
        return backend.do_reset("On")


def power_off(force: bool = False) -> dict:
    """Power off this host (``GracefulShutdown`` by default, ``ForceOff`` if force=True)."""
    with _open() as backend:
        return backend.do_reset("ForceOff" if force else "GracefulShutdown")


def power_cycle() -> dict:
    """Power-cycle this host."""
    with _open() as backend:
        return backend.do_reset("PowerCycle")


def power_reset(force: bool = False) -> dict:
    """Reset this host (``GracefulRestart`` by default, ``ForceRestart`` if force=True)."""
    with _open() as backend:
        return backend.do_reset("ForceRestart" if force else "GracefulRestart")


def get_boot_device() -> dict:
    """Return the current boot override (device/enabled/native_target)."""
    with _open() as backend:
        return backend.get_boot()


def set_boot_device(device: str = "none", persistent: bool = False) -> dict:
    """Set the boot override on this host."""
    with _open() as backend:
        return backend.set_boot(device=device, persistent=persistent)


def get_system_info() -> dict:
    """Return a normalised inventory dict for this host."""
    with _open() as backend:
        return backend.get_system_info()


def get_sensor_data() -> dict:
    """Return temperatures/fans/voltages dict for this host."""
    with _open() as backend:
        return backend.get_sensors()
