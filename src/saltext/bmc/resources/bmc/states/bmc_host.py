"""
State module for the ``bmc`` resource type.

Per-host power and boot-device states.

Example SLS::

    boot_via_http:
      bmc_host.boot_device:
        - name: bmc-host-01
        - device: http
        - persistent: false

    power_on:
      bmc_host.powered:
        - name: bmc-host-01
        - power: 'on'
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _result(name: str) -> dict:
    return {"name": name, "result": False, "comment": "", "changes": {}}


def _test(name: str, comment: str, changes: dict | None = None) -> dict:
    return {"name": name, "result": None, "comment": comment, "changes": changes or {}}


def _norm_power(power) -> str:
    if isinstance(power, bool):
        return "on" if power else "off"
    s = str(power).strip().lower()
    if s in ("on", "true", "1"):
        return "on"
    if s in ("off", "false", "0"):
        return "off"
    raise ValueError(f"Unknown power value {power!r}. Use 'on' or 'off'.")


# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------


def powered(name: str, power) -> dict:
    """
    Ensure this host is powered ``on`` or ``off``.

    :param str name: Resource ID (matches the pillar key under ``resources:bmc:``).
    :param power:    ``'on'`` or ``'off'`` (also accepts bool/0/1).

    .. code-block:: yaml

        power_on:
          bmc_host.powered:
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
        current = __salt__["bmc_host.power_status"]()  # pylint: disable=undefined-variable
    except Exception as exc:  # pylint: disable=broad-except
        ret["comment"] = f"Failed to query power status: {exc}"
        return ret

    if current == desired:
        ret["result"] = True
        ret["comment"] = f"Host '{name}' is already {desired}."
        return ret

    if __opts__.get("test"):  # pylint: disable=undefined-variable
        return _test(
            name,
            f"Host '{name}' is {current}; would set to {desired}.",
            {"power": {"old": current, "new": desired}},
        )

    try:
        if desired == "on":
            __salt__["bmc_host.power_on"]()  # pylint: disable=undefined-variable
        else:
            __salt__["bmc_host.power_off"]()  # pylint: disable=undefined-variable
    except Exception as exc:  # pylint: disable=broad-except
        ret["comment"] = f"Failed to power {desired} '{name}': {exc}"
        return ret

    ret["result"] = True
    ret["comment"] = f"Host '{name}' powered {desired}."
    ret["changes"] = {"power": {"old": current, "new": desired}}
    return ret


def boot_device(name: str, device: str, persistent: bool = False) -> dict:
    """
    Ensure this host has the given boot-device override configured.

    :param str name: Resource ID.
    :param str device: One of ``disk``, ``pxe``, ``http``, ``bios``,
                       ``cd``, ``usb``, ``none``.
    :param bool persistent: ``True`` for ``Continuous``, ``False`` for ``Once``.

    .. code-block:: yaml

        boot_via_http:
          bmc_host.boot_device:
            - name: bmc-host-01
            - device: http
            - persistent: false
    """
    ret = _result(name)
    desired_enabled = "Continuous" if persistent else "Once"

    try:
        current = __salt__["bmc_host.get_boot_device"]()  # pylint: disable=undefined-variable
    except Exception as exc:  # pylint: disable=broad-except
        ret["comment"] = f"Failed to query boot device: {exc}"
        return ret

    if current.get("device") == device.lower() and current.get("enabled") == desired_enabled:
        ret["result"] = True
        ret["comment"] = f"Host '{name}' boot device already {device!r} ({desired_enabled})."
        return ret

    if __opts__.get("test"):  # pylint: disable=undefined-variable
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
        __salt__["bmc_host.set_boot_device"](  # pylint: disable=undefined-variable
            device=device, persistent=persistent
        )
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
