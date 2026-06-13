# Example perf-controller systemd units

The agent's performance controller doesn't directly set CPU governors or
fan speeds — it just runs:

```
sudo systemctl reload-or-restart performance     # on llama wake
sudo systemctl reload-or-restart powersave       # on llama sleep
```

That delegation lets the *what* (hardware policy) live in host-tuned
systemd units, while the *when* (state transitions) lives in the agent.

If your host already has `performance.service` and `powersave.service`,
**leave them alone** — the installer will refuse to overwrite tuned units.

If your host doesn't have them, the installer can drop these examples for
you:

```bash
bash install.sh --install-perf-units
```

The example units only set the CPU governor by default. Everything else
(GPU power limits, NZXT Kraken fans, NVIDIA persistence, etc.) is
commented out in the file with explanatory comments — uncomment and tune
the lines that match your hardware.

## Files

| File | Triggered by | Default action |
|---|---|---|
| `performance.service` | llama-server wakes up | CPU governor → `performance` on every core |
| `powersave.service` | llama-server sleeps | CPU governor → `powersave` on every core |

## Extending without editing the units

Drop a systemd override snippet — it merges with the unit at runtime:

```bash
sudo systemctl edit performance       # opens an editor for /etc/systemd/system/performance.service.d/override.conf
```

Then add only the additional lines you need:

```ini
[Service]
ExecStart=/usr/bin/liquidctl --match Kraken set fan speed 70
ExecStart=/usr/bin/nvidia-smi -pl 350
```

`systemctl reload-or-restart performance` runs the merged ExecStart list
in order, so this composes cleanly with the example unit.

## Verifying

```bash
systemctl cat performance              # show merged unit + overrides
systemctl is-active performance        # 'active' after agent triggers it once
journalctl -u performance -n 20        # see the actual ExecStart output
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor   # should reflect current state
```
