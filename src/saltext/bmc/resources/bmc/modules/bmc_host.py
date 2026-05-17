"""
Execution module for the ``bmc`` resource type.

Exposes per-host BMC operations as ``bmc_host.*`` functions so they can
be invoked against individual BMC resources.  Targeting can use any of:

.. code-block:: bash

    # By resource ID alone — Salt's registry resolves the managing minion.
    salt  bmc-host-01            bmc_host.power_status
    salt  bmc-host-01            bmc_host.set_boot_device device=http persistent=False
    salt  bmc-host-01            bmc_host.power_cycle

    # By full SRN (resource type + ID).
    salt -C 'T@bmc:bmc-host-01'  bmc_host.power_status

    # All resources of type 'bmc' across the fleet.
    salt -C 'T@bmc'              bmc_host.power_status

All functions delegate to the per-resource operations defined in
:mod:`saltext.bmc.resources.bmc` via ``__resource_funcs__``.
"""

import logging

log = logging.getLogger(__name__)


def power_status():
    """
    Return ``'on'``, ``'off'``, or ``'unknown'`` for this host.

    CLI Example:

    .. code-block:: bash

        salt -C 'T@bmc:bmc-host-01' bmc_host.power_status
    """
    return __resource_funcs__["bmc.power_status"]()  # pylint: disable=undefined-variable


def power_on():
    """
    Power on this host.

    CLI Example:

    .. code-block:: bash

        salt -C 'T@bmc:bmc-host-01' bmc_host.power_on
    """
    return __resource_funcs__["bmc.power_on"]()  # pylint: disable=undefined-variable


def power_off(force=False):
    """
    Power off this host.  ``force=True`` issues ``ForceOff`` (hard cut).

    CLI Example:

    .. code-block:: bash

        salt -C 'T@bmc:bmc-host-01' bmc_host.power_off
        salt -C 'T@bmc:bmc-host-01' bmc_host.power_off force=True
    """
    return __resource_funcs__["bmc.power_off"](force=force)  # pylint: disable=undefined-variable


def power_cycle():
    """
    Power-cycle this host.

    CLI Example:

    .. code-block:: bash

        salt -C 'T@bmc:bmc-host-01' bmc_host.power_cycle
    """
    return __resource_funcs__["bmc.power_cycle"]()  # pylint: disable=undefined-variable


def power_reset(force=False):
    """
    Reset this host.  Prefers ``GracefulRestart``; ``force=True`` for ``ForceRestart``.

    CLI Example:

    .. code-block:: bash

        salt -C 'T@bmc:bmc-host-01' bmc_host.power_reset
    """
    return __resource_funcs__["bmc.power_reset"](force=force)  # pylint: disable=undefined-variable


def get_boot_device():
    """
    Return the current boot override.

    :rtype: dict — keys ``device`` (friendly name), ``redfish_target``,
            ``enabled`` (``Once``/``Continuous``/``Disabled``).

    CLI Example:

    .. code-block:: bash

        salt -C 'T@bmc:bmc-host-01' bmc_host.get_boot_device
    """
    return __resource_funcs__["bmc.get_boot_device"]()  # pylint: disable=undefined-variable


def set_boot_device(device="none", persistent=False):
    """
    Set the boot override.

    :param str device: One of ``disk``, ``pxe``, ``http``, ``bios``, ``cd``,
                       ``usb``, ``none``.
    :param bool persistent: ``True`` for ``Continuous``, ``False`` for ``Once``.

    CLI Example:

    .. code-block:: bash

        salt -C 'T@bmc:bmc-host-01' bmc_host.set_boot_device device=http
        salt -C 'T@bmc:bmc-host-01' bmc_host.set_boot_device device=pxe persistent=True
    """
    return __resource_funcs__["bmc.set_boot_device"](  # pylint: disable=undefined-variable
        device=device, persistent=persistent
    )


def get_system_info():
    """
    Return a normalised inventory dict for this host.

    :rtype: dict — keys ``manufacturer``, ``model``, ``serial_number``, ``uuid``,
            ``sku``, ``host_name``, ``bios_version``, ``firmware_version``,
            ``power_state``.

    CLI Example:

    .. code-block:: bash

        salt -C 'T@bmc:bmc-host-01' bmc_host.get_system_info
    """
    return __resource_funcs__["bmc.get_system_info"]()  # pylint: disable=undefined-variable


def get_sensor_data():
    """
    Return temperatures/fans/voltages for this host.

    :rtype: dict — ``{"temperatures": [...], "fans": [...], "voltages": [...]}``.

    CLI Example:

    .. code-block:: bash

        salt -C 'T@bmc:bmc-host-01' bmc_host.get_sensor_data
    """
    return __resource_funcs__["bmc.get_sensor_data"]()  # pylint: disable=undefined-variable
