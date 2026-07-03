# Boot persistence without systemd

If `sudo bash systemd/install_services.sh` fails with something like
*"System has not been booted with systemd as init system (PID 1). Can't
operate."* - your distro isn't running systemd (this is normal and
intentional on MX Linux, antiX, Devuan, and a few others that
deliberately use SysVinit instead). The `systemd/` unit files in this
repo simply don't apply there.

This is the equivalent for those systems: a single `@reboot` cron job
that starts everything in order, instead of four systemd units.

## Install

```bash
chmod +x sysvinit-cron/install_boot_cron.sh
sudo bash sysvinit-cron/install_boot_cron.sh
```

It'll ask where you cloned `arlo-cam-api` (same as the systemd
installer) and write a filled-in copy of `boot_all.sh.template` plus a
`/etc/cron.d/reanimarlo` entry pointing at it.

## What it does differently from the systemd units

- Sleeps 15s before doing anything, since `@reboot` can fire before your
  USB WiFi adapter has finished enumerating - systemd's unit dependency
  ordering (`After=`, device units) handles this more precisely, but cron
  has no equivalent, so a flat delay is the practical substitute.
- No automatic restart-on-crash (systemd's `Restart=on-failure` doesn't
  have a cron equivalent either). If you need that, look at process
  supervisors like `runit` or `supervisord`, though for most home setups
  the services simply not crashing in practice has been sufficient.
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
