# pydaq

[![CI](https://github.com/joergklausen/pydaq/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/joergklausen/pydaq/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-%E2%89%A53.11-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**pydaq** is a configuration-driven data-acquisition platform for atmospheric and environmental monitoring stations, running on Raspberry Pi computers and Windows/Linux PCs. It collects measurements from instruments, sensors and remote data services, stages observations and files locally, and transfers them to central storage.

The **CI badge** above reports the latest GitHub Actions result for the `main` branch. Green means the automated CI test suite passed; it does **not** certify live access to instruments, credentials, or external services. Click the badge to inspect the run.

## Capabilities

- Station-specific YAML configurations for instruments, polling schedules, outputs, networking and transfers.
- Per-instrument worker threads so a failed or slow device does not block other acquisition tasks.
- Configuration hot reload: enable, disable or change instruments without restarting the orchestrator.
- Hourly CSV or ZIP output for suitable drivers; file- and image-oriented acquisition for other sources.
- Instrument-specific minute aggregation and/or retrieval of logged records where supported.
- Staged outbox delivery via S3 and SFTP, with retries, backoff and configurable all-target success requirements.
- Startup transfer self-test, rotated logs with optional transfer, network reachability monitoring and compact console status.
- Minimal read-only HTTP dashboard for health, enabled instruments and latest measurements.
- Unit tests and explicitly opt-in live integration tests.

## Supported drivers

The following names are registered in [`pydaq/instruments/registry.py`](pydaq/instruments/registry.py). A registered driver does not imply compatibility with every firmware version, interface or station configuration.

| Instrument/source | `driver:` | Acquisition/interface |
| --- | --- | --- |
| Thermo Scientific 49C ozone analyzer | `49c` | Serial |
| Thermo Scientific 49CPS ozone analyzer | `49cps` | Thermo analyzer protocol |
| Thermo Scientific 49i ozone analyzer | `49i` | TCP/socket or configured connection |
| Palas FIDAS | `fidas` | UDP stream, with minute aggregation |
| Magee Aethalometer AE31 | `ae31` | Instrument-specific acquisition |
| Magee Aethalometer AE33 | `ae33` | Instrument-specific TCP/socket acquisition |
| Vaisala HMP ASCII sensors (including HMP60/HMP110) | `hmpascii` | Serial or TCP, including addressed/daisy-chained sensors; configurable aggregation |
| Ecotech Aurora 3000 nephelometer | `aurora3000` | Ecotech protocol (serial) |
| ACOEM/Ecotech NE300 nephelometer | `ne300` | ACOEM protocol (TCP), logged records and aggregation |
| Picarro G2401 | `g2401` | Data-log file collection plus TCP status |
| Meteorological logger / VRXA00 files | `meteo` | SFTP file retrieval |
| TAHMO / ZENTRA Cloud station | `tahmo` | HTTP API, downloadable measurement batches |
| AVO / IQAir data sources | `avo` | HTTP downloads; Parquet datasets |
| TP-Link Tapo camera (e.g. C230) | `tapo` | RTSP still-image capture via OpenCV |
| Eaton UPS | `eaton` | Read-only SNMPv3 monitoring (RFC 1628), with state-change events |

Some drivers have aliases (for example `tei49i`, `thermo49i`, `tei49c`, `thermo49c`, `tei49cps`, `thermo49cps`, and `picarro`). Prefer the canonical names in the table. Consult the driver and station configuration for the exact supported connection parameters.

Example configurations are kept at [`pydaq/configs/`](pydaq/configs/):

- `mkn.yml`: Mount Kenya (MKN), including meteorology, Thermo ozone analyzers, FIDAS, Picarro, AE33, HMP110, NE300, AVO, TAHMO, Tapo and Eaton UPS.
- `nrb.yml`: Nairobi (NRB), including Thermo ozone analyzers, FIDAS, AE31, Aurora 3000, HMP110 and AVO sources.
- `buc.yml`: Bomet University College (BUC), including a Thermo 49i.
- `vaisala.yml`: additional Vaisala-related configuration example.

These files reflect configured instrument instances, not necessarily current operational availability. Paths, device addresses and credentials must be adapted to each host.

## Repository layout

The repository keeps the acquisition platform, station configurations, tests,
and deployment utilities in one place:

```text
pydaq/
├── .github/
│   └── workflows/
│       └── ci.yml               # GitHub Actions automated tests
├── .vscode/                     # Optional project-specific editor settings
├── pydaq/                       # Python package (import pydaq)
│   ├── __init__.py
│   ├── __main__.py              # python -m pydaq entry point
│   ├── pydaq.py                 # Orchestrator, scheduler, hot reload
│   ├── dashboard.py             # HTTP JSON health/latest endpoints
│   ├── dashboard/               # Dashboard static assets
│   ├── configs/
│   │   ├── mkn.yml              # Mount Kenya
│   │   ├── nrb.yml              # Nairobi
│   │   ├── buc.yml              # Bomet University College
│   │   └── vaisala.yml
│   ├── instruments/
│   │   ├── instrument.py        # Shared base class, workers, aggregation
│   │   ├── registry.py          # Explicit driver registry (lazy imports)
│   │   ├── thermo.py            # 49C, 49CPS, 49i
│   │   ├── magee.py             # AE31, AE33
│   │   ├── fidas.py
│   │   ├── ecotech.py           # Aurora 3000, NE300
│   │   ├── vaisala.py           # HMP ASCII sensors
│   │   ├── picarro.py           # G2401
│   │   ├── meteo.py             # Meteorological file retrieval
│   │   ├── tahmo.py             # TAHMO/ZENTRA API
│   │   ├── avo.py               # AVO/IQAir data
│   │   ├── tapo.py              # RTSP camera
│   │   └── eaton.py             # UPS monitoring over SNMP
│   └── utils/
│       ├── config_handler.py    # YAML parsing and validation
│       ├── datetime_handler.py
│       ├── logging_handler.py
│       ├── storage_handler.py   # CSV rollover/outbox staging
│       ├── transfer_handler.py  # S3/SFTP transfers and retries
│       ├── network_monitor.py
│       ├── single_instance_lock.py
│       ├── status_formatter.py
│       ├── find_49i_ip_address.py
│       ├── ne300_zero_span_control.py
│       └── sftp_pull.py
├── tests/                       # Unit and live integration tests
├── deployment/                  # Deployment resources
├── cron/                        # Scheduled-task examples/resources
├── docs/                        # Additional documentation
├── pyproject.toml               # Package dependencies, tools, pytest markers
├── requirements.txt             # Historical/deployment dependency list
├── README.md
└── LICENSE
```

The listing is schematic; it shows the principal modules, rather than every
file in the repository. Note that station configurations are in
**`pydaq/configs/`**, not a repository-root `configs/` directory.

## Installation

Python **3.11 or newer** is required by `pyproject.toml`. Use an isolated virtual environment. For example, on Linux/Raspberry Pi OS:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip
git clone https://github.com/joergklausen/pydaq.git
cd pydaq
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev,s3,sftp]"
```

On Windows (PowerShell), from the cloned repository:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev,s3,sftp]"
```

Additional dependencies depend on enabled drivers; for example, `pyserial`, `requests` and `pysnmp` may be required for serial, HTTP or Eaton SNMP functions. The CI workflow installs `pyserial` and `requests` explicitly; Eaton's SNMP dependency is optional. Install only the hardware-specific packages needed on the target host.

Keep credentials in local secret files, not in YAML or Git. See
[Configuration conventions](#configuration-conventions) and
[Secrets and SSH/SFTP keys](#secrets-and-sshsftp-keys) below for the full
operational conventions.

## Configure and run

Review the chosen station YAML, especially the `station`, `paths`, `instruments`, `transfer`, `logging`, `main.dashboard` and `network_monitor` sections. The station configs live in **`pydaq/configs/`** (not a top-level `configs/` directory).

```bash
python -m pydaq -c pydaq/configs/mkn.yml
```

Other examples:

```bash
python -m pydaq -c pydaq/configs/nrb.yml
python -m pydaq -c pydaq/configs/buc.yml
```

The configured station controls which instruments are activated, their polling/rollover intervals, data and outbox directories, and transfer targets. It is normal for some drivers to require dedicated host interfaces or credentials unavailable on a development computer.

### Dashboard

When enabled in `main.dashboard`, the lightweight HTTP server exposes:

| Endpoint | Purpose |
| --- | --- |
| `/health` (or `/`) | Liveness JSON |
| `/instruments` | Instrument names registered with the running orchestrator |
| `/latest` | Latest available instrument records |

For a dashboard on port `8088`:

```bash
curl http://localhost:8088/health
curl http://localhost:8088/instruments
curl http://localhost:8088/latest
```

Non-finite numeric samples are serialized as JSON `null`, rather than invalid `NaN` values. The dashboard is a basic status API, not a secure public-facing monitoring service; restrict exposure on the network.

### Transfers and logs

- Instrument files are staged in per-instrument outboxes and uploaded by the configured transfer handler.
- S3 and SFTP targets can be individually enabled. With `transfer.require_all_targets: true`, files are retained until all enabled targets succeed.
- Transfer retries and backoff are configurable; an optional startup self-test checks configured transfer destinations.
- File logging supports size-based rotation (`logging.max_bytes`, `backup_count`) and optional staging/transfer of rotated logs via `logging.transfer`.
- Network monitoring checks configured reachable devices, without treating a UDP listener bound to `0.0.0.0` as a remote host.

## Testing and CI

Tests use **pytest**. Normal runs should not need station hardware or API credentials.

```bash
# Unit tests and other non-integration tests only
python -m pytest tests/ -m "not integration"

# Live integration tests, only on a properly configured station host
python -m pytest tests/ -m integration --run-integration --station-config pydaq/configs/mkn.yml -v -s

# Example: run only the TAHMO live probe
python -m pytest tests/test_tahmo_api_integration.py --run-integration -v -s
```

Live tests use `@pytest.mark.integration`. `tests/conftest.py` skips marked tests unless `--run-integration` (or `PYDAQ_RUN_INTEGRATION=1`) is supplied. In addition, the `-m "not integration"` selector excludes them from the routine suite. A live test can still skip when required credentials, connectivity or equipment are unavailable. For the Eaton SNMP live test, follow its documented `PYDAQ_EATON_*` environment variables.

### VS Code Test Explorer

To keep hardware-dependent tests out of the VS Code flask-icon Test Explorer, use `.vscode/settings.json`:

```json
{
  "python.testing.pytestEnabled": true,
  "python.testing.unittestEnabled": false,
  "python.testing.pytestArgs": ["tests", "-m", "not integration", "-rs"]
}
```

Refresh test discovery after saving. Run selected live probes from the terminal with `--run-integration` when required.

## Development notes

The central scheduler/orchestrator lives in `pydaq/pydaq.py`. Instrument drivers subclass the common `Instrument` class in `pydaq/instruments/instrument.py` and are resolved via the explicit lazy-import map in `pydaq/instruments/registry.py`.

To add a driver, implement its module/class, register its canonical string with a `"module:Class"` entry in `_DRIVER_MAP`, add an example YAML configuration, and write unit tests. Add separately marked live tests when hardware or external services are necessary. Prefer Google-style docstrings (`Args`, `Returns`, `Raises`) and typed interfaces.

Avoid a top-level `pydaq.py` file that would shadow the `pydaq` package. Do not commit secrets, local measurement archives, or station-specific private keys.

## Operational reference

The following short procedures are retained as field-operation mnemonics.
Some are Linux/Raspberry Pi specific; adapt interface names, IP subnets and
service names to your host.

### Check whether pydaq is running

On Linux/Raspberry Pi OS:

```bash
ps aux | grep '[p]ython.*pydaq'
pgrep -af pydaq
```

If pydaq runs as a systemd service, inspect its state and recent logs:

```bash
systemctl status pydaq.service
journalctl -u pydaq.service -n 100 --no-pager
```

Service commands apply **only if you have installed a unit named
`pydaq.service`**. For a manually launched process, use the shell/terminal
that launched `python -m pydaq` or inspect the configured log directory.
Windows users can inspect the process in Task Manager or PowerShell:

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'python.*pydaq' } | Select-Object ProcessId,CommandLine
```

### Find devices on the LAN

On a Linux host with access to the relevant local network:

```bash
sudo apt update
sudo apt install -y arp-scan nmap
sudo arp-scan --interface=eth0 --localnet
```

Use the correct active interface in place of `eth0` (for example `wlan0`).
You can identify it with `ip -br addr`. For an IP-subnet sweep, supply an
actual subnet rather than running `nmap -sn` without targets:

```bash
ip -br addr
nmap -sn 192.168.1.0/24
```

These tools discover network hosts, not USB/RS-232 devices. Firewalls and
network separation can limit what they see.

### Find serial and USB devices

Linux/Raspberry Pi OS:

```bash
lsusb -t
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
ls -l /dev/serial/by-id/ 2>/dev/null
```

The persistent `/dev/serial/by-id/` symlinks are preferable to volatile
`/dev/ttyUSB0` numbers when available. On Windows, use Device Manager
(**Ports (COM & LPT)**), or PowerShell:

```powershell
Get-CimInstance Win32_SerialPort | Select-Object DeviceID,Description
```

To identify serial ports on Linux, `ls /dev/tty*` is a device-node inventory,
not a serial equivalent of `arp-scan`: it does not probe attached instruments.

### Configuration conventions

- Station YAML files and keys are lower-case (for example `pydaq/configs/mkn.yml`).
- Configuration files refer to credentials by **path**, not by embedding secrets.
- `station`, `paths`, `logging`, `main`, `transfer`, `network_monitor`, and
  `instruments` are the major station-configuration sections.
- Each instrument has an explicit `driver` name registered in
  `pydaq/instruments/registry.py`, and can be enabled or disabled in YAML.
- Device paths, TCP/UDP addresses, ports, polling intervals, aggregation,
  rollover and transmission settings belong to the relevant instrument.
- Keep paths and credentials host-specific. Do not assume that a Windows `COM`
  device path or a private LAN IP from an example configuration is valid
  elsewhere.

The orchestrator monitors configuration changes and applies supported changes
without a restart. Review logs after a reload to ensure the selected drivers
initialized successfully.

### Secrets and SSH/SFTP keys

Conventional locations:

- `~/.ssh/` for SSH and SFTP private keys and `known_hosts`.
- `~/.secrets/` for non-SSH credentials such as S3 access keys, TAHMO/ZENTRA
  tokens, SNMP credentials and Tapo camera credentials.

On Linux/Raspberry Pi OS, create and protect the directories:

```bash
mkdir -p ~/.secrets ~/.ssh
chmod 700 ~/.secrets ~/.ssh
```

Keep private secret files accessible only to the local account that runs
pydaq (typically `chmod 600 /path/to/secret`). **Never commit them**.

For key-based SFTP, the private SSH key stays on the local acquisition
computer, while its **public** key is installed in the remote account's
`~/.ssh/authorized_keys` file. To create a pair:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -C "pydaq-sftp"
```

The private key is `~/.ssh/id_ed25519`; the shareable public key is
`~/.ssh/id_ed25519.pub`. Only the public key should be copied to the SFTP
server. Protect the private key with restrictive filesystem permissions.

### Raspberry Pi maintenance and process management

To update the OS packages deliberately (preferably when physical/network
access is available, especially for remote stations):

```bash
sudo apt update
sudo apt upgrade
```

A systemd service is generally preferable for production acquisition. A
scheduled restart is occasionally useful, but **not necessary to pick up a
YAML hot reload** and can interrupt acquisition. If an existing deployment
uses a systemd unit and a deliberate 03:00 daily restart, a single-line cron
entry looks like:

```cron
0 3 * * * /usr/bin/systemctl restart pydaq.service
```

Only use such a cron entry if that service exists, the cron user has permission
to restart it, and a daily restart is actually desired. A platform-code update
still normally requires restarting the running process to load new Python code.

### Notes on the orchestrator and drivers

`pydaq/pydaq.py` is the orchestrator. Avoid creating a **top-level**
`pydaq.py` that shadows the package import. The base class is
`pydaq/instruments/instrument.py`; it provides the common lifecycle, worker
queue and storage integration used by instrument drivers. Individual drivers
implement their own acquisition and parsing logic; not all instruments
produce the same kind of output (CSV, retrieved files, Parquet or images).

To add a new driver:

1. Add `pydaq/instruments/your_instrument.py` with a class derived from
   `Instrument` and the methods required by the base class.
2. Register the canonical driver name as a `"module:Class"` string in
   `_DRIVER_MAP` in `pydaq/instruments/registry.py` (lazy import).
3. Add a station YAML entry and unit tests, plus marked integration tests
   for hardware-dependent behavior.

Google-style docstrings (`Args:`, `Returns:`, `Raises:`) are preferred. For
VS Code's autoDocstring extension:

```json
{"autoDocstring.docstringFormat": "google"}
```

## License

Licensed under the MIT License; see [LICENSE](LICENSE).
