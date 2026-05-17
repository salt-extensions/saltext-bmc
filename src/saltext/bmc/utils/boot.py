"""
Boot-device name canonicalisation shared by all backends.

The extension uses a small set of friendly names (``disk``, ``pxe``,
``http``, ``bios``, ``cd``, ``usb``, ``none``) regardless of which BMC
protocol is in use.  Each backend translates these to its own
protocol-native value.
"""

from __future__ import annotations

CANONICAL_DEVICES = ("disk", "pxe", "http", "bios", "cd", "usb", "none")

_ALIASES = {
    "disk": "disk",
    "hdd": "disk",
    "hd": "disk",
    "pxe": "pxe",
    "network": "pxe",
    "http": "http",
    "uefihttp": "http",
    "bios": "bios",
    "bios_setup": "bios",
    "biossetup": "bios",
    "setup": "bios",
    "cd": "cd",
    "dvd": "cd",
    "usb": "usb",
    "none": "none",
    "off": "none",
    "default": "none",
}


def canonicalize(device: str) -> str:
    """
    Return the canonical friendly name for *device*.

    Raises ``ValueError`` with the list of accepted names.
    """
    key = device.strip().lower()
    try:
        return _ALIASES[key]
    except KeyError as exc:
        raise ValueError(
            f"Unknown boot device {device!r}. Accepted: {', '.join(CANONICAL_DEVICES)}"
        ) from exc
