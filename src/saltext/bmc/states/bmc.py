"""
State module for BMC (Baseboard Management Controller) hardware.

Idempotent states for power control and boot-device override.  The
underlying transport (Redfish, IPMI, or auto-detect) is selected per
profile in pillar — see :mod:`saltext.bmc.utils.backend` for details.

Example SLS::

    bmc-host-01-boot:
      bmc.boot_device:
        - name: bmc-host-01
        - device: http
        - persistent: false

    bmc-host-01-power:
      bmc.powered:
        - name: bmc-host-01
        - power: 'on'
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

__virtualname__ = "bmc"


def __virtual__():
    return __virtualname__


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _result(name: str) -> dict:
    return {"name": name, "result": False, "comment": "", "changes": {}}


def _test(name: str, comment: str, changes: dict | None = None) -> dict:
    return {"name": name, "result": None, "comment": comment, "changes": changes or {}}


def _norm_power(power) -> str:
    """Accept on/off/True/False/1/0 and normalize to 'on'/'off'."""
    if isinstance(power, bool):
        return "on" if power else "off"
    s = str(power).strip().lower()
    if s in ("on", "true", "1"):
        return "on"
    if s in ("off", "false", "0"):
        return "off"
    raise ValueError(f"Unknown power value {power!r}. Use 'on' or 'off'.")


# ----------------------------------------------------------------------
# States
# ----------------------------------------------------------------------


def powered(name: str, power, **conn) -> dict:
    """
    Ensure the BMC's host is powered ``on`` or ``off``.

    :param str name: BMC profile name (key under ``saltext.bmc:profiles``)
                     or arbitrary identifier when explicit connection
                     kwargs are supplied.
    :param power:    Desired state: ``'on'`` or ``'off'`` (also accepts
                     bool/0/1).

    Connection kwargs (``host``, ``username``, ``password``, ``verify_ssl``)
    may be passed through; otherwise the pillar profile under ``name`` is
    used.

    .. code-block:: yaml

        my-host-power:
          bmc.powered:
            - name: bmc-host-01
            - power: 'on'
    """
    ret = _result(name)
    try:
        desired = _norm_power(power)
    except ValueError as exc:
        ret["comment"] = str(exc)
        return ret

    try:
        current = __salt__["bmc.power_status"](name, **conn)
    except Exception as exc:  # pylint: disable=broad-except
        ret["comment"] = f"Failed to query power status: {exc}"
        return ret

    if current == desired:
        ret["result"] = True
        ret["comment"] = f"Host '{name}' is already {desired}."
        return ret

    if __opts__.get("test"):
        return _test(
            name,
            f"Host '{name}' is {current}; would set to {desired}.",
            {"power": {"old": current, "new": desired}},
        )

    try:
        if desired == "on":
            __salt__["bmc.power_on"](name, **conn)
        else:
            __salt__["bmc.power_off"](name, **conn)
    except Exception as exc:  # pylint: disable=broad-except
        ret["comment"] = f"Failed to power {desired} '{name}': {exc}"
        return ret

    ret["result"] = True
    ret["comment"] = f"Host '{name}' powered {desired}."
    ret["changes"] = {"power": {"old": current, "new": desired}}
    return ret


def boot_device(name: str, device: str, persistent: bool = False, **conn) -> dict:
    """
    Ensure the BMC has the given boot-device override configured.

    :param str name: BMC profile name.
    :param str device: One of ``disk``, ``pxe``, ``http``, ``bios``,
                       ``cd``, ``usb``, ``none``.
    :param bool persistent: ``True`` for ``Continuous``, ``False`` for ``Once``.

    .. code-block:: yaml

        my-host-boot:
          bmc.boot_device:
            - name: bmc-host-01
            - device: http
            - persistent: false
    """
    ret = _result(name)
    desired_enabled = "Continuous" if persistent else "Once"

    try:
        current = __salt__["bmc.get_boot_device"](name, **conn)
    except Exception as exc:  # pylint: disable=broad-except
        ret["comment"] = f"Failed to query boot device: {exc}"
        return ret

    if current.get("device") == device.lower() and current.get("enabled") == desired_enabled:
        ret["result"] = True
        ret["comment"] = f"Host '{name}' boot device already {device!r} ({desired_enabled})."
        return ret

    if __opts__.get("test"):
        return _test(
            name,
            f"Would set boot device to {device!r} ({desired_enabled}); "
            f"current: {current.get('device')!r} ({current.get('enabled')}).",
            {
                "device": {"old": current.get("device"), "new": device.lower()},
                "enabled": {"old": current.get("enabled"), "new": desired_enabled},
            },
        )

    try:
        __salt__["bmc.set_boot_device"](name, device=device, persistent=persistent, **conn)
    except Exception as exc:  # pylint: disable=broad-except
        ret["comment"] = f"Failed to set boot device on '{name}': {exc}"
        return ret

    ret["result"] = True
    ret["comment"] = f"Host '{name}' boot device set to {device!r} ({desired_enabled})."
    ret["changes"] = {
        "device": {"old": current.get("device"), "new": device.lower()},
        "enabled": {"old": current.get("enabled"), "new": desired_enabled},
    }
    return ret
