# Rebooting BMC-managed hosts

This extension's reboot states differ from `system.reboot` in one important way: they run on a controlling minion against a remote box's BMC, so the calling minion stays up while the target reboots. That makes it possible to **wait** for the target to come back inside the same state run — something a `system.reboot` state can't do, because the minion itself goes away.

## The two-layer problem

"The host has rebooted" can mean two different things:

1. **The BMC reports `power = on`.** Fast — usually within seconds of a reset. Says nothing about whether the OS is up.
2. **The OS is actually serviceable** — pingable, SSH responsive, a salt-minion reconnected, etc.

`bmc_host.rebooted` (resource-style) and `bmc.rebooted` (profile-style) cover both:

- Issue a reset (graceful by default, `force: true` for a hard reset).
- Sleep `initial_delay` seconds so the reset has time to begin (otherwise the first poll may still see the pre-reset `on` state and return early).
- Poll the BMC's power state until it reports `on`, up to `timeout` seconds.
- If `os_host` + `os_port` are supplied, open a TCP probe against them and poll until the OS accepts a connection, up to `os_timeout` seconds.

Transient errors during the BMC's own restart — TLS resets, 401s while its web stack restarts, brief HTTP timeouts — are caught per poll and counted as a non-matching result rather than aborting the wait.

## Resource-style (recommended)

If your hosts are declared as `bmc` resources in Pillar, target them by resource ID:

```yaml
reboot_host:
  bmc_host.rebooted:
    - name: bmc-host-01      # the bmc resource ID
    - force: false
    - timeout: 600           # wait up to 10m for BMC to report power=on
    - initial_delay: 5
    - os_host: 10.0.0.5      # OS-side IP (often distinct from BMC IP)
    - os_port: 22            # SSH; pick whatever proves the OS is up
    - os_timeout: 300        # then wait up to 5m for SSH
```

To gate later states on the reboot completing:

```yaml
reboot_host:
  bmc_host.rebooted:
    - name: bmc-host-01
    - os_host: 10.0.0.5
    - os_port: 22

provision_after_reboot:
  salt.state:
    - tgt: bmc-host-01
    - sls: baremetal.provision
    - require:
      - bmc_host: reboot_host
```

## Profile-style

If you're using the older `saltext.bmc:profiles:` Pillar layout instead of `resources:bmc:`, the equivalent state is `bmc.rebooted` and takes the same options plus the usual connection-override kwargs:

```yaml
reboot_legacy_host:
  bmc.rebooted:
    - name: bmc-host-01
    - force: true
    - timeout: 600
    - os_host: 10.0.0.5
    - os_port: 22
```

## Waiting without rebooting

If a reboot was triggered some other way and you just want to wait for the box to come back, the underlying execution function is exposed directly:

```bash
# Resource-style:
salt -C 'T@bmc:bmc-host-01' bmc_host.wait_for_power state=on timeout=600

# Profile-style:
salt-call --local bmc.wait_for_power bmc-host-01 state=on timeout=600
```

Both return a dict:

```python
{
    "result": True,         # False on timeout
    "state": "on",          # last observed state ('unknown' if errors throughout)
    "target": "on",         # requested state
    "polls": 7,             # number of polls performed
    "elapsed": 32.4,        # seconds spent waiting
    "error": None,          # last exception message if any
}
```

`state` can be `'on'` or `'off'` — useful for waiting on a `force: true` shutdown to finish before flipping a downstream switch.

## Choosing timeouts

- **BMC power-on (`timeout`)** — typically 5–10 minutes on real hardware. Some servers (especially Dells doing POST self-tests, or hosts running long memory training) need 15+. Default is 600s.
- **`initial_delay`** — 5s is enough for any modern Redfish BMC to register the reset and start reporting the transition. Bump to 10–15s on legacy IPMI BMCs that lag.
- **OS probe (`os_timeout`)** — depends on what the OS does during boot. Plain Linux to SSH is usually under 90s; a host that runs filesystem checks, decrypts disks, or waits for cloud-init can easily need 5+ minutes.
- **`interval` / `os_interval`** — 5s defaults strike a balance between responsiveness and BMC/network load. Don't drop below 2s.

## Notes on idempotence

`rebooted` is **not** idempotent — applying it repeatedly will reboot the host repeatedly. Use it under `onchanges` / `watch` or behind a `require_in` guard so it only fires when something upstream actually changed:

```yaml
firmware_update:
  bmc_host.set_boot_device:
    - name: bmc-host-01
    - device: http

apply_firmware_reboot:
  bmc_host.rebooted:
    - name: bmc-host-01
    - os_host: 10.0.0.5
    - os_port: 22
    - onchanges:
      - bmc_host: firmware_update
```
