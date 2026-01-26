"""Abstract base class for pydaq instruments.

Responsibilities
---------------
- Load its own YAML config and expose relevant properties.
- Provide optional serial and socket helpers for subclasses.
- Buffer data (text or polars DataFrame) and save to timestamped files.
- Stage files and optionally transfer via SFTP.
- Remain thread-safe when save and sample overlap (lightweight locks).
"""

from __future__ import annotations

import functools
import logging
import threading
import time
import zipfile
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Optional

# Optional serial support for serial instruments
try:
    import serial  # type: ignore
    from serial import SerialException, SerialTimeoutException  # type: ignore
except Exception:  # pragma: no cover
    serial = None  # allow import without pyserial

# Optional Polars support for parquet output
try:
    import polars as pl  # type: ignore
except Exception:  # pragma: no cover
    pl = None

from utils.config import load_config  # type: ignore


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


# def with_serial(func):
#     """Decorator to open/close a serial port around a method (thread-safe, lazy).

#     Expects the instance to carry `_serial_port` and `_serial_cfg` populated by __init__.
#     """
#     def wrapper(self, *args, **kwargs):
#         if serial is None:
#             raise RuntimeError("pyserial is not available; cannot use serial communication.")
#         # Serialize device access per instrument
#         with self._io_lock:
#             if getattr(self, "_serial", None) is None:
#                 port = getattr(self, "_serial_port", None)
#                 cfg = getattr(self, "_serial_cfg", None)
#                 if not port or not isinstance(cfg, dict):
#                     raise RuntimeError("Serial port/config not initialized on Instrument.")
#                 self._serial = serial.Serial(
#                     port=port,
#                     baudrate=cfg.get("baudrate", 9600),
#                     bytesize=cfg.get("bytesize", 8),
#                     parity=cfg.get("parity", "N"),
#                     stopbits=cfg.get("stopbits", 1),
#                     timeout=cfg.get("timeout", 2),
#                 )
#             if not self._serial.is_open:
#                 self._serial.open()
#             try:
#                 return func(self, *args, **kwargs)
#             finally:
#                 try:
#                     self._serial.close()
#                 except Exception:
#                     pass
#     return wrapper
def with_serial(func):
    """Decorator to handle serial communication with retries and cooldown.

    Expects the instance to carry `_serial_port` and `_serial_cfg` populated by __init__.
    Lazily opens the serial port on first use.
    """
    @functools.wraps(func)
    def wrapper(self, *args, retries: int = 3, **kwargs) -> str:
        if serial is None:
            raise RuntimeError("pyserial is not available; cannot use serial communication.")

        # cooldown gate
        now = time.time()
        if getattr(self, "_cooldown_until", 0.0) > now:
            return ""

        # non-overlapping I/O
        if not self._io_lock.acquire(blocking=False):
            return ""

        try:
            last_err: Exception | None = None
            for i in range(retries):
                try:
                    # Lazily create/open serial port
                    if getattr(self, "_serial", None) is None:
                        port = getattr(self, "_serial_port", None)
                        cfg = getattr(self, "_serial_cfg", None)
                        if not port or not isinstance(cfg, dict):
                            raise RuntimeError("Serial port/config not initialized on Instrument.")
                        self._serial = serial.Serial(
                            port=port,
                            baudrate=cfg.get("baudrate", 9600),
                            bytesize=cfg.get("bytesize", 8),
                            parity=cfg.get("parity", "N"),
                            stopbits=cfg.get("stopbits", 1),
                            timeout=cfg.get("timeout", 2),
                        )
                    if not self._serial.is_open:
                        self._serial.open()

                    # one attempt: protocol-specific code
                    result = func(self, *args, **kwargs)

                    # success → reset fail counters / cooldown
                    self._fail_count = 0
                    self._cooldown_until = 0.0
                    return result
                except (SerialTimeoutException, SerialException, OSError) as err:
                    last_err = err
                    self.logger.error(
                        f"[{getattr(self, 'name', getattr(self, '_name', 'instrument'))}] "
                        f"serial_comm attempt {i+1}/{retries} failed: {err}"
                    )
                    try:
                        if getattr(self, "_serial", None) is not None and self._serial.is_open:
                            self._serial.close()
                    except Exception:
                        pass

                    self._fail_count = getattr(self, "_fail_count", 0) + 1
                    max_fail = getattr(self, "_max_fail_before_cooldown", 5)
                    cooldown = getattr(self, "_cooldown_seconds", 120)
                    if self._fail_count >= max_fail:
                        self._cooldown_until = time.time() + cooldown
                        self.logger.error(
                            f"[{getattr(self, 'name', getattr(self, '_name', 'instrument'))}] "
                            f"communication failing repeatedly; backing off for {cooldown}s."
                        )
                        break

                    # simple exponential backoff, capped
                    time.sleep(min(0.5 * (2 ** i), 3.0))

            # all retries failed
            if last_err is not None:
                self.logger.error(
                    f"[{getattr(self, 'name', getattr(self, '_name', 'instrument'))}] "
                    f"giving up after {retries} attempts."
                )
            return ""
        finally:
            self._io_lock.release()
    return wrapper


class Instrument(ABC):
    """
    Abstract base providing shared behavior for pydaq instruments.
    
    Properties:
    - sampling_interval_seconds: int
        Sampling interval in seconds.
    - aggregation_period: int
        Aggregation period in minutes.
    - reporting_interval: int
        Reporting interval in minutes (staging and transfer cadence).
    - remote_prefix: str
        Prefix under which this instrument stages remotely.
        - For S3: becomes the key prefix (e.g., '<default_prefix>/<remote_prefix>/file.zip').
        - For SFTP: appended to the configured remote base path.
        Defaults to the instrument name if not set in YAML.

    Abstract methods to implement in subclasses:
    - _serial_comm(cmd: str) -> str
        Low-level serial command/response; driver defines protocol details.
    - _socket_comm(cmd: str) -> str
        Low-level socket command/response; driver defines protocol details.

    - get_config() -> dict
        Return mapping of configuration items → values read from device.
    - set_config() -> dict
        Apply configuration commands and return mapping cmd → response.
    - set_datetime() -> None
        Set the instrument's internal date/time (if supported).

    - display_data() -> None
        Acquire simple data from the instrument and display on console.

    - get_data() -> str
        Acquire data from the instrument and typically call `accumulate_data`.
    - accumulate_data(data: str) -> None
        Append a line of text data to the internal buffer.
    - accumulate_dataframe(df_like: Any) -> None
        Append a Polars DataFrame (or DataFrame-like) to the parquet buffer.

    - save_data_file() -> None
        Persist buffered data to disk (text or parquet), then reset the buffer.
    - stage_data_file() -> None
        Copy or ZIP the last saved file into the staging folder.
    - transfer_files() -> bool
        Upload staged files via S3 (preferred) or SFTP (fallback), depending on what is configured in the global YAML.
        Returns True if any backend ran without raising; False otherwise.
    """

    # ---------- lifecycle ----------

    def __init__(self, name: str, config_path: str) -> None:
        """Load config, set up paths and communications, and initialize buffers."""
        self._name = name
        self._config_path = config_path
        cfg = load_config(config_path)
        self._general = cfg
        self.logger = logging.getLogger(f"pydaq.{name}")

        # Paths
        local = cfg.get("local", {})
        root = Path(local.get("root", "."))
        self.data_path = (root / local.get("data", "data")).expanduser()
        self.staging_path = (root / local.get("staging", "staging")).expanduser()
        _ensure_dir(self.data_path)
        _ensure_dir(self.staging_path)

        # Instruments section can be list entries or mapping keyed by name
        instr_section = cfg.get("instruments", {})
        if isinstance(instr_section, list):
            entry = next((i for i in instr_section if i.get("name") == name), {})
            params = entry.get("params", {}) or {}
            if "model" in entry and "model" not in params:
                params["model"] = entry["model"]
            if "communication" in entry and "communication" not in params:
                params["communication"] = entry["communication"]
            self._params = params
        else:
            self._params = instr_section.get(name, {})

        # Execution mode
        self.simulate: bool = bool(self._params.get("simulate", cfg.get("simulate", False)))

        # Metadata & comms
        try:
            self._id = int(self._params.get("id", 0))
        except Exception:
            self._id = 0
        self._serial_number: Optional[str] = self._params.get("serial_number")
        self._params_comms = str(self._params.get("communication", "serial")).lower()

        # Serial (lazy connection info)
        self._serial: Any = None
        self._serial_port: Optional[str] = None
        self._serial_cfg: Dict[str, Any] = {}
        if self._params_comms == "serial":
            port = self._params.get("serial", "COM1")
            ports_cfg = cfg.get("ports", {})
            port_cfg = ports_cfg.get(
                port, {"baudrate": 9600, "bytesize": 8, "parity": "N", "stopbits": 1, "timeout": 2}
            )
            self._serial_port = port
            self._serial_cfg = dict(port_cfg)

        # Socket params (driver decides how to use them)
        sock = self._params.get("socket", {}) or {}
        self._sockmode: str = str(sock.get("mode", "tcp")).lower()
        self._sockaddr = (sock.get("host", "127.0.0.1"), int(sock.get("port", 0)))
        self._socktout: float = float(sock.get("timeout", 2))
        self._socksleep: float = float(sock.get("sleep", 0.1))

        # Scheduling + files
        self._sampling_interval: int = int(self._params.get("sampling_interval_seconds", 60))
        self._aggregation_period: int = int(self._params.get("sampling_interval_seconds", 60))
        self._reporting_interval: int = int(self._params.get("reporting_interval_minutes", 60))
        self._staging_zip: bool = bool(self._params.get("staging_zip", True))
        self._file_timestamp_format: str = "%Y%m%d%H%M"  # minute precision avoids overwrite

        # File format & header
        filename_extension = self._params.get("filename_extension", "txt")
        if isinstance(filename_extension, str) and filename_extension.lower() == "none":
            filename_extension = None
        self._filename_extension: Optional[str] = filename_extension  # dat|txt|parquet|None
        default_header = f"# {self._name} S/N={self._serial_number} id={self._id}"
        self._header: str = str(self._params.get("header", default_header))

        # Buffers
        self._data: str = ""
        self._df_buffer: Any = None  # Polars DataFrame if parquet
        self._saved_data_path: Optional[Path] = None

        # Locks to make overlapping save/get safe
        self._io_lock = threading.RLock()
        self._buf_lock = threading.RLock()

        # Transfer config
        self._transfer = cfg.get("transfer", {})

    # ---------- public properties ----------

    @property
    def sampling_interval_seconds(self) -> int:
        """Sampling interval in seconds."""
        return self._sampling_interval

    @property
    def aggregation_period(self) -> int:
        """Aggregation period in minutes."""
        return self._aggregation_period
    
    @property
    def reporting_interval(self) -> int:
        """Reporting interval in minutes (staging and transfer cadence)."""
        return self._reporting_interval

    @property
    def remote_prefix(self) -> str:
        """
        Prefix under which this instrument stages remotely.
        - For S3: becomes the key prefix (e.g., '<default_prefix>/<remote_prefix>/file.zip').
        - For SFTP: appended to the configured remote base path.
        Defaults to the instrument name if not set in YAML.
        """
        # Accept either key in YAML, fall back to the instrument name
        return str(self._params.get("remote_path") or self._params.get("key_prefix") or self._name)

    def _use_serial(self) -> bool:
        """
        Return True if this instrument is configured to use serial communications.

        Subclasses can override this if they want dynamic switching.
        """
        return self._params_comms == "serial"

    # ---------- abstract I/O the driver must provide ----------

    @abstractmethod
    def get_data(self) -> str:
        """Acquire data from the instrument and typically call `accumulate_data`."""
        ...

    @abstractmethod
    def display_data(self) -> None:
        """Acquire simple data from the instrument and display on console."""
        ...

    @abstractmethod
    def get_config(self) -> dict:
        """Return mapping of configuration items → values read from device."""
        ...

    @abstractmethod
    def set_config(self) -> dict:
        """Apply configuration commands and return mapping cmd → response."""
        ...

    @abstractmethod
    def set_datetime(self) -> None:
        """Set the instrument's internal date/time (if supported)."""
        ...

    @abstractmethod
    def _serial_comm(self, cmd: str) -> str:
        """Low-level serial command/response; driver defines protocol details."""
        ...

    @abstractmethod
    def _socket_comm(self, cmd: str) -> str:
        """Low-level socket command/response; driver defines protocol details."""
        ...

    @abstractmethod
    def accumulate_data(self, data: str) -> None:
        """Append a line of text data to the internal buffer."""
        ...

    # ---------- parquet helper ----------

    def accumulate_dataframe(self, df_like: Any) -> None:
        """Append a Polars DataFrame (or DataFrame-like) to the parquet buffer."""
        if pl is None:
            raise RuntimeError("polars is required for accumulate_dataframe/save parquet.")
        df = df_like if isinstance(df_like, pl.DataFrame) else pl.DataFrame(df_like)
        with self._buf_lock:
            self._df_buffer = df if self._df_buffer is None else self._df_buffer.vstack(df, in_place=False)

    # ---------- file operations ----------

    def _build_target_path(self) -> Path:
        """Return the full path for the next save file based on timestamp and extension."""
        ts = datetime.now().strftime(self._file_timestamp_format)
        base = f"{self._name}-{ts}"
        return self.data_path / (f"{base}.{self._filename_extension}" if self._filename_extension else base)

    def save_data_file(self) -> None:
        """Persist buffered data to disk (text or parquet), then reset the buffer."""
        target = self._build_target_path()
        ext = (self._filename_extension or "").lower()

        if ext == "parquet":
            if pl is None:
                self.logger.error("polars not available; cannot save parquet.")
                return
            with self._buf_lock:
                df = self._df_buffer
                self._df_buffer = None
            if df is None or (hasattr(df, "height") and df.height == 0):
                self.logger.info("No dataframe data to save; skipping save_data_file().")
                return
            if target.suffix.lower() != ".parquet":
                target = target.with_suffix(".parquet")
            df.write_parquet(target, compression="zstd")
        else:
            with self._buf_lock:
                data = self._data
                self._data = ""
            if not data:
                self.logger.info("No data to save; skipping save_data_file().")
                return
            content = (self._header + "\n" if self._header else "") + data.rstrip("\n") + "\n"
            target.write_text(content, encoding="utf-8")

        self._saved_data_path = target
        self.logger.info("Saved data file: %s", target)

    def stage_data_file(self) -> None:
        """Copy or ZIP the last saved file into the staging folder."""
        if not self._saved_data_path or not self._saved_data_path.exists():
            self.logger.warning("No saved data file to stage.")
            return
        src = self._saved_data_path
        dst = self.staging_path / src.name

        if self._staging_zip:
            zip_dst = dst.with_suffix(".zip")
            with zipfile.ZipFile(zip_dst, "w") as zf:
                zf.write(src, arcname=src.name)
            self.logger.info("Staged %s → %s", src, zip_dst)
            return

        dst.write_bytes(src.read_bytes())
        self.logger.info("Staged %s → %s", src, dst)

    # ---------- transfer (optional) ----------

    def transfer_files(self) -> bool:
        """
        Upload staged files via S3 (preferred) or SFTP (fallback),
        depending on what is configured in the global YAML.

        Returns
        -------
        bool
            True if any backend ran without raising; False otherwise.
        """
        ok_any = False

        # --- S3 first (preferred) ---
        s3_cfg = self._general.get("s3", {})
        if s3_cfg:
            try:
                from utils.s3fsc import S3FSC  # your helper module
                s3 = S3FSC(
                    config=self._general,
                    use_proxies=bool(s3_cfg.get("use_proxies", True)),
                    addressing_style=s3_cfg.get("addressing_style", "path"),
                    verify=s3_cfg.get("verify", True),
                    default_prefix=s3_cfg.get("default_prefix", ""),
                )
                # Push the whole staging directory under <default_prefix>/<remote_prefix>/
                key_prefix = str(PurePosixPath(s3_cfg.get("default_prefix", "")) / self.remote_prefix)
                # Prefer not removing local files when using S3 to keep your ZIP as audit trail
                s3.transfer_files(local_path=str(self.staging_path), key_prefix=key_prefix, remove_on_success=False)
                self.logger.info("S3 transfer done: staging=%s prefix=%s", self.staging_path, key_prefix)
                ok_any = True
            except Exception as err:
                self.logger.error("S3 transfer failed: %s", err)

        # --- SFTP (fallback or in addition, if you want both) ---
        sftp_cfg = (self._general.get("transfer", {}) or {}).get("sftp", {})
        if sftp_cfg and sftp_cfg.get("host"):
            try:
                from utils.sftp import SFTPClient
            except Exception as err:
                self.logger.error("SFTP client unavailable: %s", err)
            else:
                try:
                    client = SFTPClient(
                        config={
                            "host": sftp_cfg.get("host"),
                            "usr": sftp_cfg.get("usr") or sftp_cfg.get("user"),
                            "key_path": sftp_cfg.get("key_path") or sftp_cfg.get("key"),
                            "passphrase": sftp_cfg.get("passphrase"),
                            "password": sftp_cfg.get("password"),
                            "staging": str(self.staging_path),
                            "remote": sftp_cfg.get("remote") or sftp_cfg.get("remote_path"),
                            "accept_unknown_host_keys": sftp_cfg.get("accept_unknown_host_keys", True),
                            "timeouts": sftp_cfg.get("timeouts", {}),
                        },
                        logger=self.logger,
                    )
                    # Put under <remote>/<remote_prefix>/...
                    remote_dir = PurePosixPath(client.remote_base) / self.remote_prefix  # type: ignore[attr-defined]
                    count = client.transfer_files(remove_on_success=True, local_path=self.staging_path, remote_path=remote_dir)
                    self.logger.info("SFTP transfer done: %s files to %s", count, remote_dir)
                    ok_any = True or ok_any
                except Exception as err:
                    self.logger.error("SFTP transfer failed: %s", err)

        if not ok_any:
            self.logger.warning("No transfer backend configured or all failed.")
        return ok_any
    
