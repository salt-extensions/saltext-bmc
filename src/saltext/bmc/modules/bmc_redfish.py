"""
Low-level Redfish HTTP passthrough.

Bypasses the backend abstraction in :mod:`saltext.bmc.modules.bmc` to let
operators read or write any Redfish endpoint directly.  Useful for
ad-hoc inspection, vendor-specific OEM extensions, and one-off
operations that the high-level module does not cover.

This module is Redfish-only.  If the profile's ``backend`` is set to
``ipmi``, calls raise :class:`~saltext.bmc.utils.redfish.RedfishError`
immediately.

CLI examples (``path`` is the first positional arg; pass the profile via
``name=``)::

    salt-call --local bmc_redfish.get   /redfish/v1/         name=bmc-host-01
    salt-call --local bmc_redfish.get   /redfish/v1/Systems  name=bmc-host-01
    salt-call --local bmc_redfish.patch /redfish/v1/Systems/1 \\
        body='{"AssetTag": "rack-7-slot-3"}' name=bmc-host-01
    salt-call --local bmc_redfish.post \\
        /redfish/v1/Systems/1/Actions/ComputerSystem.Reset \\
        body='{"ResetType": "On"}' name=bmc-host-01
"""

from __future__ import annotations

import logging

from saltext.bmc.utils import redfish as rf

log = logging.getLogger(__name__)

__virtualname__ = "bmc_redfish"


def __virtual__():
    return __virtualname__


def _require_redfish(name: str | None, conn: dict) -> None:
    """Raise if the resolved profile uses a non-Redfish backend."""
    cfg = rf.resolve_conn(__opts__, name=name, **conn)
    backend = (cfg.get("backend") or "redfish").lower()
    if backend == "ipmi":
        raise rf.RedfishError(
            f"Profile {name!r} uses backend={backend!r}; bmc_redfish.* requires Redfish."
        )


def _client(name: str | None, conn: dict) -> rf.RedfishClient:
    _require_redfish(name, conn)
    return rf.open_client(__opts__, name=name, **conn)


def get(path: str, name: str | None = None, **conn) -> dict:
    """
    Raw Redfish GET.

    :param str path: Redfish path (must start with ``/redfish/v1/``).
    :param str name: Profile name; falls back to top-level config.

    CLI Example:

    .. code-block:: bash

        salt-call --local bmc_redfish.get /redfish/v1/Systems name=bmc-host-01
    """
    with _client(name, conn) as client:
        return client.get(path)


def post(path: str, name: str | None = None, body: dict | None = None, **conn) -> dict | None:
    """
    Raw Redfish POST.

    :param str path: Redfish action path.
    :param dict body: Request body (optional for some Redfish actions).
    :param str name: Profile name.

    CLI Example:

    .. code-block:: bash

        salt-call --local bmc_redfish.post \\
            /redfish/v1/Systems/1/Actions/ComputerSystem.Reset \\
            body='{"ResetType": "On"}' name=bmc-host-01
    """
    with _client(name, conn) as client:
        return client.post(path, body)


def patch(path: str, body: dict, name: str | None = None, **conn) -> dict | None:
    """
    Raw Redfish PATCH.

    :param str path: Redfish resource path.
    :param dict body: Request body (required).
    :param str name: Profile name.

    CLI Example:

    .. code-block:: bash

        salt-call --local bmc_redfish.patch /redfish/v1/Systems/1 \\
            body='{"AssetTag": "rack-7-slot-3"}' name=bmc-host-01
    """
    with _client(name, conn) as client:
        return client.patch(path, body)


def delete(path: str, name: str | None = None, **conn) -> dict | None:
    """
    Raw Redfish DELETE.

    :param str path: Redfish resource path.
    :param str name: Profile name.

    CLI Example:

    .. code-block:: bash

        salt-call --local bmc_redfish.delete \\
            /redfish/v1/SessionService/Sessions/3 name=bmc-host-01
    """
    with _client(name, conn) as client:
        return client.delete(path)
