# Boot persistence without systemd

If `sudo bash systemd/install_services.sh` fails with something like
*"System has not been booted with systemd as init system (PID 1). Can't
operate."* - your distro isn't running systemd (this is normal and
intentional on MX Linux, antiX, Devuan, and a few others that
deliberately use SysVinit instead). The `systemd/` unit files in this
repo simply don't apply there.

This is the equivalent for those systems: a `@reboot` cron job that
starts everything in order (`boot_all.sh`), plus a second cron job that
runs every 2 minutes and restarts anything that's crashed since
(`watchdog.sh`) - since `@reboot` alone only fires once, at boot, and
wouldn't notice or recover from a mid-session crash the way systemd's
`Restart=on-failure` does natively.

## Install

```bash
chmod +x sysvinit-cron/install_boot_cron.sh
sudo bash sysvinit-cron/install_boot_cron.sh
```

It'll ask where you cloned `arlo-cam-api` (same as the systemd
installer) and write filled-in copies of `boot_all.sh.template` and
`watchdog.sh.template`, plus a `/etc/cron.d/reanimarlo` with both
schedules.

## What it does differently from the systemd units

- Sleeps 15s before doing anything at boot, since `@reboot` can fire
  before your USB WiFi adapter has finished enumerating - systemd's unit
  dependency ordering (`After=`, device units) handles this more
  precisely, but cron has no equivalent, so a flat delay is the practical
  substitute.
- Crash recovery is a separate polling script (`watchdog.sh`, every 2
  minutes) rather than something built into the process supervisor
  itself, since cron doesn't have a per-job restart-on-failure concept.
  Verified end-to-end: killing the viewer process got it detected and
  restarted within one cron cycle.
- Runs `server.py` and `app.py` via `su - youruser -c "..."` since cron
  jobs run as root by default and these shouldn't run as root.

## Known gotcha this surfaced: PATH under `su`

A normal interactive shell's `PATH` usually doesn't include `/sbin` or
`/usr/sbin` (only root's does, by default) - but that's where `iw` lives
on most distros. `viewer/app.py`'s signal-strength lookups already work
around this (they resolve the actual binary path explicitly rather than
trusting the inherited `PATH`), but if you're troubleshooting some other
command failing only when launched through this boot path and not when
you run it by hand in a terminal, this is a good first thing to check.
