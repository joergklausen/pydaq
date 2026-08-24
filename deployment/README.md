# pydaq deployment assets

This directory contains operating-system launch/supervision files.  Station
runtime configuration remains under `pydaq/configs/`; launchers and scheduler
files do not belong there because they describe deployment rather than the
instrument/station model.

## Layout

- `windows/pydaq.bat` — thin synchronous Windows launcher.
- `windows/pydaq.xml` — MKN Task Scheduler definition.
- `linux/pydaq.sh` — thin synchronous Linux launcher.
- `linux/crontab.example` — minimal `@reboot` example for existing cron-based installations.

The application itself owns single-instance protection.  Launchers must not
attempt to identify Python processes with `ps`, `pgrep`, PowerShell CIM queries,
or PID files.

## Windows / Task Scheduler

Import `windows/pydaq.xml` on the current MKN Windows client, or reproduce its
settings in Task Scheduler.  The important differences from the previous task
are:

- one **At log on** trigger; no hourly repetition;
- **Do not start a new instance** (`IgnoreNew`);
- restart up to five times at one-minute intervals after an actual task failure;
- no 72-hour forced stop (`ExecutionTimeLimit=PT0S`);
- do not stop/refuse the DAQ when Windows reports battery operation;
- explicit repository working directory;
- action points to `deployment/windows/pydaq.bat`.

The XML is deliberately station/machine-specific because Task Scheduler exports
contain account and filesystem details.  If the repository is used on another
Windows station, copy it and adjust the account, repository path, and config
argument.

## Linux / cron

For an existing cron-based host, install the single `@reboot` line from
`linux/crontab.example`, adapted to the station. Do not schedule pydaq every few
minutes as a duplicate-start watchdog. The application lock prevents damage,
but repeated launch attempts create needless processes and logs.

For production Linux deployments, a `systemd` service with `Restart=on-failure`
is preferable to cron because it supervises the long-running process directly.
