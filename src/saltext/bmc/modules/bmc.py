"""
Execution module for BMC (Baseboard Management Controller) hardware.

Supports two protocol backends — Redfish and IPMI (via ``pyghmi``) —
selected per profile by a ``backend`` key.  The default is ``auto``,
which probes Redfish first and falls back to IPMI.  See
:mod:`saltext.bmc.utils.backend` for backend selection details.

Configuration via Pillar::

    saltext.bmc:
      profiles:
        bmc-host-01:
          host: 10.0.0.5
          username: root
          password: calvin
          verify_ssl: false
          # backend defaults to 'auto'; set explicitly to skip the probe:
          # backend: redfish    # or 'ipmi'
        legacy-host:
          host: 10.0.0.7
          username: ADMIN
          password: ADMIN
          backend: ipmi
          port: 623           # IPMI-only

CLI examples::

    salt-call --local bmc.power_status     bmc-host-01
    salt-call --local bmc.set_boot_device  bmc-host-01 device=http persistent=False
    salt-call --local bmc.power_cycle      bmc-host-01
    salt-call --local bmc.get_system_info  bmc-host-01
    salt-call --local bmc.get_sensor_data  bmc-host-01

    # Bypass pillar with explicit connection kwargs:
    salt-call --local bmc.power_status host=10.0.0.5 username=root password=calvin verify_ssl=False
    salt-call --local bmc.power_status host=10.0.0.7 username=ADMIN password=ADMIN backend=ipmi
"""

from __future__ import annotations

import logging

from saltext.bmc.utils import backend as bk
from saltext.bmc.utils import boot as boot_canon
from saltext.bmc.utils import wait as wait_util

log = logging.getLogger(__name__)

__virtualname__ = "bmc"


def __virtual__():
    return __virtualname__


# ----------------------------------------------------------------------
# Power operations
# ----------------------------------------------------------------------


def power_status(name: str | None = None, **conn) -> str:
    """
    Return the current power state: ``'on'``, ``'off'``, or ``'unknown'``.

    :param name: BMC profile name (key under ``saltext.bmc:profiles``).
                 Omit to use the top-level ``saltext.bmc`` config.

    CLI Example:

    .. code-block:: bash

        salt-call --local bmc.power_status bmc-host-01
    """
    with bk.open_backend(__opts__, name=name, **conn) as backend:
        return backend.power_status()


def power_on(name: str | None = None, **conn) -> dict:
    """
    Power on the host.

    CLI Example:

    .. code-block:: bash

        salt-call --local bmc.power_on bmc-host-01
    """
    with bk.open_backend(__opts__, name=name, **conn) as backend:
        return backend.do_reset("On")


def power_off(name: str | None = None, force: bool = False, **conn) -> dict:
    """
    Power off the host.

    :param force: If True, issue ``ForceOff`` (hard cut).  Otherwise
                  ``GracefulShutdown`` is attempted.

    CLI Example:

    .. code-block:: bash

        salt-call --local bmc.power_off bmc-host-01
        salt-call --local bmc.power_off bmc-host-01 force=True
    """
    with bk.open_backend(__opts__, name=name, **conn) as backend:
        return backend.do_reset("ForceOff" if force else "GracefulShutdown")


def power_cycle(name: str | None = None, **conn) -> dict:
    """
    Power-cycle the host (off then on).

    CLI Example:

    .. code-block:: bash

        salt-call --local bmc.power_cycle bmc-host-01
    """
    with bk.open_backend(__opts__, name=name, **conn) as backend:
        return backend.do_reset("PowerCycle")


def power_reset(name: str | None = None, force: bool = False, **conn) -> dict:
    """
    Reset the host.  Prefers ``GracefulRestart``; pass ``force=True`` for ``ForceRestart``.

    CLI Example:

    .. code-block:: bash

        salt-call --local bmc.power_reset bmc-host-01
    """
    with bk.open_backend(__opts__, name=name, **conn) as backend:
        return backend.do_reset("ForceRestart" if force else "GracefulRestart")


def wait_for_power(
    name: str | None = None,
    state: str = "on",
    timeout: int = 600,
    interval: int = 5,
    **conn,
) -> dict:
    """
    Poll the BMC until reported power matches ``state`` or ``timeout`` elapses.

    A fresh BMC connection is opened per poll so transient errors during a
    reboot (TLS resets, 401s while the BMC restarts its web stack) don't
    abort the wait — they're counted as a non-matching result and retried.

    :param state:    ``'on'`` or ``'off'``.
    :param timeout:  Maximum seconds to wait.
    :param interval: Seconds between polls.

    :returns: dict with keys ``result`` (bool), ``state`` (last observed),
              ``target`` (requested), ``polls``, ``elapsed``, ``error``.

    CLI Example:

    .. code-block:: bash

        salt-call --local bmc.wait_for_power bmc-host-01 state=on timeout=600
    """
    target = str(state).strip().lower()
    if target not in ("on", "off"):
        raise ValueError(f"state must be 'on' or 'off', got {state!r}")

    def _status():
        with bk.open_backend(__opts__, name=name, **conn) as backend:
            return backend.power_status()

    ok, last, polls, elapsed = wait_util.poll_until(
        fn=_status,
        predicate=lambda v: v == target,
        timeout=timeout,
        interval=interval,
    )
    is_exc = isinstance(last, Exception)
    return {
        "result": ok,
        "state": "unknown" if is_exc else last,
        "target": target,
        "polls": polls,
        "elapsed": round(elapsed, 2),
        "error": str(last) if is_exc else None,
    }


# ----------------------------------------------------------------------
# Boot-device operations
# ----------------------------------------------------------------------


def get_boot_device(name: str | None = None, **conn) -> dict:
    """
    Return the current one-shot/persistent boot override.

    Returns a dict with keys ``device`` (friendly name), ``redfish_target``,
    ``native_target``, and ``enabled`` (``Once``/``Continuous``/``Disabled``).
    On IPMI backends ``redfish_target`` is ``None``.

    CLI Example:

    .. code-block:: bash

        salt-call --local bmc.get_boot_device bmc-host-01
    """
    with bk.open_backend(__opts__, name=name, **conn) as backend:
        return backend.get_boot()


def set_boot_device(
    name: str | None = None,
    device: str = "none",
    persistent: bool = False,
    **conn,
) -> dict:
    """
    Set the boot override.

    :param str device: One of ``disk``, ``pxe``, ``http``, ``bios``, ``cd``,
                       ``usb``, ``none``.  ``http`` and ``usb`` are not
                       supported by the IPMI backend.
    :param bool persistent: If True, override persists across reboots
                            (Redfish ``Continuous``).  Otherwise it is
                            consumed on the next boot (``Once``).

    CLI Example:

    .. code-block:: bash

        salt-call --local bmc.set_boot_device bmc-host-01 device=http
        salt-call --local bmc.set_boot_device bmc-host-01 device=pxe persistent=True
    """
    # Validate the device name up-front so we fail fast without opening a
    # connection on a typo.
    boot_canon.canonicalize(device)
    with bk.open_backend(__opts__, name=name, **conn) as backend:
        return backend.set_boot(device=device, persistent=persistent)


# ----------------------------------------------------------------------
# Inventory and sensors
# ----------------------------------------------------------------------


def get_system_info(name: str | None = None, **conn) -> dict:
    """
    Return a normalised inventory dict.

    Keys: ``manufacturer``, ``model``, ``serial_number``, ``uuid``, ``sku``,
    ``host_name``, ``bios_version``, ``firmware_version``, ``power_state``.
    Unknown fields are returned as ``None``.

    CLI Example:

    .. code-block:: bash

        salt-call --local bmc.get_system_info bmc-host-01
    """
    with bk.open_backend(__opts__, name=name, **conn) as backend:
        return backend.get_system_info()


def get_sensor_data(name: str | None = None, **conn) -> dict:
    """
    Return ``{"temperatures": [...], "fans": [...], "voltages": [...]}``.

    Each sensor entry has ``name``, ``reading``, ``unit``, ``status``.
    A backend may return an empty list for a sensor class it does not
    expose.

    CLI Example:

    .. code-block:: bash

        salt-call --local bmc.get_sensor_data bmc-host-01
    """
    with bk.open_backend(__opts__, name=name, **conn) as backend:
        return backend.get_sensors()
