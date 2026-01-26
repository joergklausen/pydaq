# pydaq

A lightweight, `schedule`-based **data acquisition platform** for deploying station-specific instrument parks
on Raspberry Pi 4/5 and small industrial PCs.

The platform supports:
- enable/disable instruments via config hot reload (no restart required)
- per-instrument worker threads (one instrument can't block the whole app)
- (currently hourly) file rollover and outbox staging
- outbox transmission via S3/SFTP with retry/backoff

A **dashboard** with tiny HTTP JSON endpoints is included for health checks and latest data retrieval.

This repository is structured so that **platform code** can be updated without entangling station configs and
deployment artifacts.

## Repository layout

```
pydaq/                  # Python package (import pydaq)
configs/                # station YAML configs (e.g. mkn.yml, nrb.yml)
examples/               # example configs, demo scripts, sample data
tests/                  # pytest test suite
pyproject.toml          # packaging + dependencies + tooling
README.md
LICENSE
```

Package internals:

```
pydaq/
├─ __init__.py
├─ __main__.py          # `python -m pydaq -c configs/mkn.yml`
├─ main.py              # orchestrator (scheduler + hot reload + worker threads)
├─ dashboard.py         # tiny HTTP JSON endpoints (stdlib only)
├─ instruments/
│  ├─ instrument.py     # abstract base class + worker-thread queue
│  ├─ registry.py       # driver registry (lazy imports)
│  ├─ thermo.py         # stubs (replace with real drivers)
│  ├─ neph.py
│  ├─ ae33.py
│  ├─ vaisala.py
│  ├─ g2401.py
│  ├─ fidas.py
│  ├─ meteo.py
│  └─ tapo.py
└─ utils/
   ├─ config_handler.py          # config schema + YAML loader
   ├─ datetime_handler.py
   ├─ logging_handler.py
   ├─ storage_handler.py         # hourly CSV rollover + outbox staging (CSV or ZIP)
   └─ transfer_handler.py        # S3 + SFTP outbox upload with retries/backoff
```

## Usage

```bash
python -m pydaq -c configs/mkn.yml
```

Dashboard endpoints (if enabled in config):
- `GET /health`
- `GET /instruments`
- `GET /latest`

Example:
- `http://localhost:8088/latest`

In addition, the dashboard serves static files from `pydaq/dashboard/static/`.

In operations, use systemd or similar to manage the process. A **crontab** job can be used to periodically restart
the process to pick up platform updates (e.g. daily at 3am):
```
0 3 * * * /usr/bin/systemctl restart pydaq.service
```

## Configuration conventions

This repo assumes:
- **config file names are lower-case** (e.g. `configs/mkn.yml`)
- **config content keys are lower-case**
- configs do **not** contain secrets; they contain **paths** to secrets (e.g. `~/.ssh/...`, `~/.secrets/...`)

## Secrets convention

- `~/.ssh/` for SSH keys (standard tooling expects it).
- `~/.secrets/` for non-SSH secrets, e.g.
  - `~/.secrets/s3_access_key_id`
  - `~/.secrets/s3_secret_access_key`
  - `~/.secrets/tapo-account-username`
  - `~/.secrets/tapo-account-password`

Make sure secret files have strict permissions (typically `chmod 600`).

### Public/private key SSH/sftp pairs
The user's private key is kept secret and stored locally on the user's machine (=client) while the user's public key is uploaded and registered on the machine the user connects to (=server). The public key can be shared freely, the private key must not be exchanged or shared. To generate a public/private key pair, use:

```bash
ssh-keygen -t ed25519 -b 4096 -f ~/.ssh/id_ed25519 -C "
```
The public key will be stored in `~/.ssh/id_ed25519.pub` and the private key in `~/.ssh/id_ed25519` on the client machine. The public key content must be added to the server's `~/.ssh/authorized_keys` file to enable key-based authentication. The permissions of the `~/.ssh` directory and its contents should be set to ensure security (e.g., `chmod 700 ~/.ssh` and `chmod 600 ~/.ssh/authorized_keys`).

## Notes on `main.py`

`pydaq/main.py` is the orchestrator module.
Avoid creating a *top-level* `main.py` that shadows package imports unintentionally.
(Repo root scripts are fine, but keep the orchestrator inside the package.)

## Notes on instrument drivers

The **abstract base class** is in `pydaq/instruments/instrument.py`. It provides abstract methods that model the data life-cycle. This consists of 
- instrument initialization including reading the existing configuration and setting the desired configuration;
- periodic data acquisition and parsing;
- appending parsed data records to a local CSV file via a thread-safe worker-thread queue and CSV data storage functionality. 
- (currently hourly) file rollover and staging for outbox transmission;
- outbox transmission via S3/SFTP with retry/backoff.

The **driver registry** is in `pydaq/instruments/registry.py`. It maps driver names (strings) to driver classes.

To create a new **instrument driver**:
1. Create a new module `pydaq/instruments/your_instrument.py`.
2. Create a new class `YourInstrument(Instrument)` that implements the abstract methods.
3. Register the new driver class in `pydaq/instruments/registry.py` by adding
   ```python
   from pydaq.instruments.your_instrument import YourInstrument
   ```
   and adding an entry to the `_DRIVER_REGISTRY` dictionary:
   ```python
   _DRIVER_REGISTRY: Dict[str, Type[Instrument]] = {
       ...
       "your_instrument": YourInstrument,
   }
   ```

## Docstrings

The codebase uses **Google-style** docstrings (``Args:``, ``Returns:``, ``Raises:``) because it stays readable
in editors and works well with type hints.

In the VS Code **autoDocstring** extension, set:

```json
{ "autoDocstring.docstringFormat": "google" }
```
