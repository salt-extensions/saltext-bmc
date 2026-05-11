# saltext-bmc — Plan

## What is this?

A Salt extension for managing **BMC** (Baseboard Management Controller) hardware.

The BMC is an independent microcontroller embedded on a server motherboard. It has its own
CPU, RAM, network port, and power supply — it runs even when the server is powered off. It is
the hardware component that enables out-of-band server management: power control, boot order,
console access, and hardware monitoring without needing the main OS to be running.

**IPMI** (Intelligent Platform Management Interface) and **Redfish** are *protocols* for
talking to the BMC — they are not the BMC itself. This extension is named `saltext-bmc`
because it manages the device regardless of which protocol is used underneath.

## Why build this?

### Immediate use case: automated bare metal ESXi installation

The goal is to automate bare metal ESXi installation via UEFI HTTP Boot using Salt. The
workflow involves three Salt extensions working together:

```
saltext-opsdev  →  saltext-bmc  →  saltext-vmware
```

1. **`saltext-opsdev` (nimbus_baremetal resource)** — provisions a physical machine from
   Nimbus (Broadcom's internal lab infrastructure) and surfaces its BMC credentials as a
   Salt resource.

2. **`saltext-bmc`** — uses those credentials to configure the machine's boot order to
   UEFI HTTP Boot and power-cycles the machine, causing it to pull the ESXi installer
   from an HTTP server.

3. **`saltext-vmware` (esxi_install state)** — sets up the HTTP boot server (nginx serving
   unpacked ESXi ISO content), generates a kickstart file (`ks.cfg`) for unattended
   installation, and waits for the host to come online after install completes.

### Why this approach instead of existing tooling?

- **Salt already ships `salt.modules.ipmi`** but it is effectively unmaintained, covers
  only basic power operations, and has no Redfish support.
- **Redfish is the modern standard** (DMTF, 2015+) used by all major vendors. It is
  REST/HTTPS/JSON, meaning no binary protocol libraries, firewall-friendly, and easy to
  debug. IPMI is legacy (UDP 623, binary) but still common in lab environments.
- A proper `saltext-bmc` with Redfish as the primary backend and IPMI as a fallback
  covers the full range of hardware likely to be encountered in Nimbus pods and real
  datacenters.

### Broader value

BMC/IPMI management is completely hardware-generic — nothing VMware-specific about it.
A well-built `saltext-bmc` is useful for:
- Any bare metal provisioning workflow (not just ESXi)
- Hardware CI/CD pipelines that need to power-cycle DUTs (devices under test)
- Compliance checks on server firmware and BIOS settings
- Replacing one-off `ipmitool` shell calls with idempotent Salt states

## Protocol backends

| Protocol | Transport    | Hardware era   | Notes                              |
|----------|-------------|----------------|------------------------------------|
| Redfish  | REST/HTTPS  | 2018+ (most)  | DMTF standard, preferred           |
| IPMI     | UDP 623     | Legacy/labs    | `pyipmi` or `python-ipmi` library  |

Vendor-specific REST APIs (Dell iDRAC, HP iLO, Supermicro) are all Redfish-compliant at
their core, so the Redfish backend covers them without special-casing.

The extension should detect which backend is available at connection time (try Redfish
first, fall back to IPMI) or allow explicit configuration.

## Module scope (v0.1)

### Execution modules

**`bmc` (primary execution module)**
```
bmc.power_status   → on | off | unknown
bmc.power_on
bmc.power_off
bmc.power_cycle
bmc.power_reset    → graceful OS-level reset (if supported)

bmc.get_boot_device  → disk | pxe | http | bios | usb | none
bmc.set_boot_device  disk | pxe | http | bios | usb
                      [persistent=False]  → one-shot by default

bmc.get_system_info  → dict: manufacturer, model, serial, firmware
bmc.get_sensor_data  → dict: temps, fan speeds, voltages

bmc.sol_activate     → attach serial-over-LAN console (blocking)
```

**`bmc_redfish` (Redfish-specific, lower-level)**
```
bmc_redfish.get   path → raw JSON for any Redfish endpoint
bmc_redfish.post  path body
bmc_redfish.patch path body
```

### State modules

**`bmc`**
```yaml
# Ensure a machine is powered on with one-shot HTTP boot set
my-host:
  bmc.powered:
    - name: my-host
    - power: on

my-host-boot:
  bmc.boot_device:
    - name: my-host
    - device: http
    - persistent: false
```

### Resource type (Salt 3008+)

A `bmc` resource type so that BMC credentials can be managed via Salt Pillar and passed
between states without repeating connection config:

```yaml
# Pillar
resources:
  bmc:
    nimbus-host-01:
      host: 10.10.10.5
      username: root
      password: calvin
      backend: redfish        # or: ipmi
      verify_ssl: false
```

## Connection config (Pillar pattern)

Following the same pattern as `saltext-vmware`:

```yaml
saltext.bmc:
  backend: redfish            # default
  host: 192.168.1.100
  username: root
  password: calvin
  verify_ssl: false

# Multiple profiles
saltext.bmc:
  profiles:
    nimbus-host-01:
      host: 10.0.0.5
      username: admin
      password: secret
    nimbus-host-02:
      host: 10.0.0.6
      username: admin
      password: secret
```

## Directory layout

```
saltext-bmc/
├── PLAN.md                          ← this file
├── pyproject.toml
├── setup.py
├── noxfile.py
├── changelog/
└── src/
    └── saltext/
        └── bmc/
            ├── __init__.py
            ├── modules/
            │   ├── bmc.py           ← primary execution module
            │   └── bmc_redfish.py   ← low-level Redfish helpers
            ├── states/
            │   └── bmc.py           ← powered, boot_device states
            ├── resources/
            │   └── bmc.py           ← Salt 3008+ resource type
            └── utils/
                ├── __init__.py
                ├── redfish.py       ← Redfish session/auth helpers
                └── ipmi.py          ← IPMI helpers (pyipmi wrapper)
```

## Python dependencies

```
salt >= 3008.0rc1
requests >= 2.31.0          # Redfish (pure HTTP)
pyipmi >= 0.19              # IPMI fallback (optional)
```

`pyipmi` is optional — if not installed, only the Redfish backend is available.

## Build order

1. Scaffold repo (pyproject.toml, noxfile, pre-commit — copy from saltext-vmware)
2. `utils/redfish.py` — session management, GET/POST/PATCH helpers, error handling
3. `utils/ipmi.py` — thin wrapper over `pyipmi`, same interface as redfish utils
4. `modules/bmc.py` — power and boot device ops, auto-detect backend
5. `modules/bmc_redfish.py` — raw Redfish passthrough
6. `states/bmc.py` — powered, boot_device (idempotent)
7. `resources/bmc.py` — Salt resource type for Pillar-driven BMC profiles
8. Unit tests for each module
9. Install into resources venv, wire up with saltext-opsdev nimbus_baremetal
