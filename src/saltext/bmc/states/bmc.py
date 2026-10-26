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
import time

from saltext.bmc.utils import wait as wait_util

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


def rebooted(
    name: str,
    force: bool = False,
    timeout: int = 600,
    interval: int = 5,
    initial_delay: int = 5,
    os_host: str | None = None,
    os_port: int | None = None,
    os_timeout: int = 300,
    os_interval: int = 5,
    os_connect_timeout: int = 5,
    **conn,
) -> dict:
    """
    Reset the BMC's host and wait for it to come back online.

    The state issues a reset, then polls the BMC until power returns to
    ``on``.  If ``os_host`` and ``os_port`` are supplied, a TCP probe runs
    afterwards to confirm the operating system is reachable — BMC
    ``power=on`` only proves the chassis is powered, not that the OS is up.

    :param str name:        BMC profile name.
    :param bool force:      Issue ``ForceRestart`` instead of ``GracefulRestart``.
    :param int timeout:     Seconds to wait for BMC power to report ``on``.
    :param int interval:    Seconds between BMC polls.
    :param int initial_delay: Seconds to sleep after issuing the reset before
                              polling.  Prevents a same-second poll seeing
                              the pre-reset ``on`` state and returning early.
    :param str os_host:     OS-side hostname/IP to TCP-probe.  None to skip.
    :param int os_port:     OS-side TCP port (e.g. 22 for SSH).
    :param int os_timeout:  Seconds to wait for the OS-side probe.
    :param int os_interval: Seconds between OS-side probes.
    :param int os_connect_timeout: Per-attempt TCP connect timeout.

    .. code-block:: yaml

        reboot_host:
          bmc.rebooted:
            - name: bmc-host-01
            - force: false
            - timeout: 600
            - os_host: 10.0.0.5
            - os_port: 22
            - os_timeout: 300
    """
    ret = _result(name)

    if __opts__.get("test"):
        msg = f"Would reset host '{name}' (force={force}) and wait up to {timeout}s for power"
        if os_host and os_port:
            msg += f", then up to {os_timeout}s for TCP {os_host}:{os_port}"
        return _test(name, msg + ".")

    try:
        __salt__["bmc.power_reset"](name, force=force, **conn)
    except Exception as exc:  # pylint: disable=broad-except
        ret["comment"] = f"Failed to issue reset on '{name}': {exc}"
        return ret

    if initial_delay > 0:
        time.sleep(initial_delay)

    power = __salt__["bmc.wait_for_power"](
        name, state="on", timeout=timeout, interval=interval, **conn
    )
    changes: dict = {
        "power": {"old": "reset", "new": power["state"]},
        "polls": power["polls"],
        "elapsed": power["elapsed"],
    }
    if not power["result"]:
        ret["comment"] = (
            f"Reset issued on '{name}' but BMC did not report power 'on' within "
            f"{timeout}s (last={power['state']}, polls={power['polls']})."
        )
        ret["changes"] = changes
        return ret

    if os_host and os_port:
        ok, _, polls, elapsed = wait_util.poll_until(
            fn=lambda: wait_util.tcp_probe(os_host, os_port, os_connect_timeout),
            predicate=lambda v: v is True,
            timeout=os_timeout,
            interval=os_interval,
        )
        changes["os_probe"] = {
            "host": os_host,
            "port": os_port,
            "polls": polls,
            "elapsed": round(elapsed, 2),
            "reachable": ok,
        }
        if not ok:
            ret["comment"] = (
                f"Host '{name}' powered on, but {os_host}:{os_port} not reachable "
                f"within {os_timeout}s (polls={polls})."
            )
            ret["changes"] = changes
            return ret

    ret["result"] = True
    ret["comment"] = f"Host '{name}' rebooted."
    ret["changes"] = changes
    return ret
