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

import logging
import threading
import zipfile
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Optional

try:
    import serial  # type: ignore
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


def with_serial(func):
    """Decorator to open/close a serial port around a method (thread-safe, lazy).

    Expects the instance to carry `_serial_port` and `_serial_cfg` populated by __init__.
    """
    def wrapper(self, *args, **kwargs):
        if serial is None:
            raise RuntimeError("pyserial is not available; cannot use serial communication.")
        # Serialize device access per instrument
        with self._io_lock:
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
            try:
                return func(self, *args, **kwargs)
            finally:
                try:
                    self._serial.close()
                except Exception:
                    pass
    return wrapper


class Instrument(ABC):
    """Abstract base providing shared behavior for pydaq instruments."""

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
        self._serial = None
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
        self._sampling_interval: int = int(self._params.get("sampling_interval", 60))
        self._aggregation_period: int = int(self._params.get("sampling_interval", 1))
        self._reporting_interval: int = int(self._params.get("reporting_interval", 60))
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
    def sampling_interval(self) -> int:
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
    
    # def transfer_files(self) -> bool:
    #     """Upload staged files via SFTP if `transfer.sftp` is configured."""
    #     sftp_cfg = self._transfer.get("sftp", {})
    #     if not sftp_cfg or not sftp_cfg.get("host"):
    #         self.logger.info("No SFTP config found; skipping transfer.")
    #         return False
    #     try:
    #         # If your SFTP helper lives elsewhere, adjust this import.
    #         from utils.sftp import SFTPClient  # type: ignore
    #     except Exception as err:
    #         self.logger.error("SFTP not available: %s", err)
    #         return False

    #     cfg = {
    #         "host": sftp_cfg.get("host"),
    #         "usr": sftp_cfg.get("usr") or sftp_cfg.get("user"),
    #         "key_path": sftp_cfg.get("key_path") or sftp_cfg.get("key"),
    #         "passphrase": sftp_cfg.get("passphrase"),
    #         "password": sftp_cfg.get("password"),
    #         "staging": str(self.staging_path),
    #         "remote": sftp_cfg.get("remote") or sftp_cfg.get("remote_path"),
    #         "accept_unknown_host_keys": sftp_cfg.get("accept_unknown_host_keys", True),
    #         "timeouts": sftp_cfg.get("timeouts", {}),
    #     }
    #     try:
    #         client = SFTPClient(config=cfg, logger=self.logger)
    #         client.transfer_files(remove_on_success=True)
    #         return True
    #     except Exception as err:
    #         self.logger.error("SFTP transfer failed: %s", err)
    #         return False
        

# class Instrument(ABC):
#     """Abstract base for instruments with scheduling and file ops."""

#     # ---- lifecycle ----
#     def __init__(self, name: str, config_path: str):
#         self._name = name
#         self._config_path = config_path
#         cfg = load_config(config_path)
#         self._general = cfg
#         self.logger = _get_logger(f"pydaq.{name}")

#         # paths
#         local = cfg.get("local", {})
#         root = Path(local.get("root", "."))
#         self.data_path = (root / local.get("data", "data")).expanduser()
#         self.staging_path = (root / local.get("staging", "staging")).expanduser()
#         ensure_dir(self.data_path)
#         ensure_dir(self.staging_path)

#         # instruments section can be dict keyed by name OR list of entries
#         instr_section = cfg.get("instruments", {})
#         if isinstance(instr_section, list):
#             entry = next((i for i in instr_section if i.get("name") == name), {})
#             params = entry.get("params", {})
#             if "model" in entry and "model" not in params:
#                 params["model"] = entry["model"]
#             if "communication" in entry and "communication" not in params:
#                 params["communication"] = entry["communication"]
#             self._params = params
#         else:
#             self._params = instr_section.get(name, {})

#         # execution mode
#         self.simulate: bool = bool(self._params.get("simulate", cfg.get("simulate", False)))

#         # metadata & comms
#         try:
#             self._id = int(self._params.get("id", 0))
#         except Exception:
#             self._id = 0
#         self._serial_number: Optional[str] = self._params.get("serial_number")
#         self._params_comms = str(self._params.get("communication", "serial")).lower()

#         # serial (lazy config only)
#         self._serial: Optional[Any] = None
#         self._serial_port: Optional[str] = None
#         self._serial_cfg: Dict[str, Any] = {}
#         if self._params_comms == "serial":
#             port = self._params.get("serial", "COM1")
#             ports_cfg = cfg.get("ports", {})
#             port_cfg = ports_cfg.get(port, {"baudrate": 9600, "bytesize": 8, "parity": "N", "stopbits": 1, "timeout": 2})
#             self._serial_port = port
#             self._serial_cfg = dict(port_cfg)

#         # sockets
#         sock = self._params.get("socket", {})
#         self._sockmode: str = str(sock.get("mode", "tcp")).lower()
#         self._sockaddr = (sock.get("host", "127.0.0.1"), int(sock.get("port", 0)))
#         self._socktout: float = float(sock.get("timeout", 2))
#         self._socksleep: float = float(sock.get("sleep", 0.1))

#         # scheduling
#         self._averaging_interval: int = int(self._params.get("averaging_interval", 1))
#         self._reporting_interval: int = int(self._params.get("reporting_interval", 60))
#         self._staging_zip: bool = bool(self._params.get("staging_zip", True))
#         self._file_timestamp_format: str = "%Y%m%d%H"  # adjusted in setup_schedules()

#         # filename/header controls (simple)
#         filename_extension = self._params.get("filename_extension", "txt")
#         if isinstance(filename_extension, str) and filename_extension.lower() == "none":
#             filename_extension = None
#         self._filename_extension: Optional[str] = filename_extension  # one of dat|txt|parquet|None
#         default_header = f"# {self._name} S/N={self._serial_number} id={self._id}"
#         self._header: str = str(self._params.get("header", default_header))

#         # buffers
#         self._data: str = ""
#         self._df_buffer: Any = None  # Polars DataFrame for parquet
#         self._saved_data_path: Optional[Path] = None

#         # transfer
#         self._transfer = cfg.get("transfer", {})

#     # ---- abstract I/O ----
#     @abstractmethod
#     def get_data(self) -> str: ...
#     @abstractmethod
#     def accumulate_data(self, data: str) -> None: ...
#     @abstractmethod
#     def _serial_comm(self, cmd: str) -> str: ...
#     @abstractmethod
#     def _socket_comm(self, cmd: str) -> str: ...
#     @abstractmethod
#     def set_datetime(self) -> None: ...
#     @abstractmethod
#     def get_config(self) -> dict: ...
#     @abstractmethod
#     def set_config(self) -> dict: ...

#     # ---- helpers for parquet instruments ----
#     def accumulate_dataframe(self, df_like: Any) -> None:
#         if pl is None:
#             raise RuntimeError("polars is required for accumulate_dataframe/save parquet.")
#         df = df_like if isinstance(df_like, pl.DataFrame) else pl.DataFrame(df_like)
#         if self._df_buffer is None:
#             self._df_buffer = df
#         else:
#             self._df_buffer = self._df_buffer.vstack(df, in_place=False)

#     # ---- file ops ----
#     def _build_target_path(self) -> Path:
#         ts = datetime.now().strftime(self._file_timestamp_format)
#         base = f"{self._name}-{ts}"
#         if self._filename_extension:
#             return self.data_path / f"{base}.{self._filename_extension}"
#         return self.data_path / base

#     def save_data_file(self) -> None:
#         target = self._build_target_path()
#         ext = (self._filename_extension or "").lower()

#         if ext == "parquet":
#             if pl is None:
#                 self.logger.error("polars not available; cannot save parquet.")
#                 return
#             if self._df_buffer is None or (hasattr(self._df_buffer, 'height') and self._df_buffer.height == 0):
#                 self.logger.info("No dataframe data to save; skipping save_data_file().")
#                 return
#             if target.suffix.lower() != ".parquet":
#                 target = target.with_suffix(".parquet")
#             self._df_buffer.write_parquet(target, compression="zstd")
#             self._df_buffer = None
#         else:
#             if not self._data:
#                 self.logger.info("No data to save; skipping save_data_file().")
#                 return
#             if self._header:
#                 content = f"{self._header}\n{self._data}.rstrip('\n')\n"
#             else:
#                 content = f"{self._data}.rstrip('\n')\n"

#             target.write_text(content, encoding="utf-8")
#             self._data = ""

#         self._saved_data_path = target
#         self.logger.info("Saved data file: %s", target)

#     def stage_data_file(self) -> None:
#         if not self._saved_data_path or not self._saved_data_path.exists():
#             self.logger.warning("No saved data file to stage.")
#             return
#         src = self._saved_data_path
#         dst = self.staging_path / src.name

#         if self._staging_zip:
#             zip_dst = dst.with_suffix(".zip")
#             with zipfile.ZipFile(zip_dst, "w") as zf:
#                 zf.write(src, arcname=src.name)
#             self.logger.info("Staged %s → %s", src, zip_dst)
#         else:
#             dst.write_bytes(src.read_bytes())
#             self.logger.info("Staged %s → %s", src, dst)

#     # ---- transfer (SFTP adapter, optional) ----
#     def transfer_files(self) -> bool:
#         sftp_cfg = self._transfer.get("sftp", {})
#         if not sftp_cfg or not sftp_cfg.get("host"):
#             self.logger.info("No SFTP config found; skipping transfer.")
#             return False
#         try:
#             from utils.sftp import SFTPClient  # type: ignore
#         except Exception as err:
#             self.logger.error("SFTP not available: %s", err)
#             return False

#         cfg = {
#             "host": sftp_cfg.get("host"),
#             "usr": sftp_cfg.get("usr") or sftp_cfg.get("user"),
#             "key_path": sftp_cfg.get("key_path") or sftp_cfg.get("key"),
#             "passphrase": sftp_cfg.get("passphrase"),
#             "password": sftp_cfg.get("password"),
#             "staging": str(self.staging_path),
#             "remote": sftp_cfg.get("remote") or sftp_cfg.get("remote_path"),
#             "accept_unknown_host_keys": sftp_cfg.get("accept_unknown_host_keys", True),
#             "timeouts": sftp_cfg.get("timeouts", {}),
#         }
#         try:
#             client = SFTPClient(config=cfg, logger=self.logger)
#             client.transfer_files(remove_on_success=True)
#             return True
#         except Exception as err:
#             self.logger.error("SFTP transfer failed: %s", err)
#             return False

#     # ---- scheduling ----
#     def setup_schedules(self) -> bool:
#         try:
#             schedule.every(self._averaging_interval).minutes.do(self.get_data)
#             schedule.every(self._averaging_interval).minutes.do(self.save_data_file)

#             if self._reporting_interval == 10:
#                 self._file_timestamp_format = "%Y%m%d%H%M"
#                 for m in (0, 10, 20, 30, 40, 50):
#                     schedule.every().hour.at(f":{m:02}").do(self.stage_data_file)
#             elif self._reporting_interval >= 1440:
#                 self._file_timestamp_format = "%Y%m%d"
#                 schedule.every().day.at("00:00").do(self.stage_data_file)
#             else:
#                 self._file_timestamp_format = "%Y%m%d%H"
#                 schedule.every().hour.at(":00").do(self.stage_data_file)
#             return True
#         except Exception as err:
#             self.logger.error("setup_schedules failed: %s", err)
#             return False

# # instr/instrument.py
# from __future__ import annotations

# import logging
# import zipfile
# from abc import ABC, abstractmethod
# from datetime import datetime
# from pathlib import Path
# from typing import Optional, Any, Dict, Callable, TypeVar, cast

# import schedule

# try:
#     import serial  # type: ignore
# except Exception:  # pragma: no cover
#     serial = None  # allows import without pyserial

# # Your project loader; we keep this import to fit your existing layout
# from utils.config import load_config  # type: ignore


# T = TypeVar("T")


# def _get_logger(name: str) -> logging.Logger:
#     lg = logging.getLogger(name)
#     if not lg.handlers:
#         h = logging.StreamHandler()
#         fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
#         h.setFormatter(fmt)
#         lg.addHandler(h)
#         lg.setLevel(logging.INFO)
#     return lg


# def ensure_dir(path: Path) -> None:
#     path.mkdir(parents=True, exist_ok=True)


# def timestamp(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
#     return datetime.now().strftime(fmt)


# def with_serial(func: Callable[..., T]) -> Callable[..., T]:
#     """Decorator for methods that need an open serial port.
#     - Creates the Serial object on demand using self._serial_port/_serial_cfg
#     - Opens before call, closes afterwards (even on error)
#     """
#     def wrapper(self, *args, **kwargs) -> T:  # type: ignore[override]
#         if serial is None:
#             raise RuntimeError("pyserial is not available; cannot use serial communication.")
#         # Build serial object lazily
#         if getattr(self, "_serial", None) is None:
#             port = getattr(self, "_serial_port", None)
#             cfg = getattr(self, "_serial_cfg", None)
#             if not port or not isinstance(cfg, dict):
#                 raise RuntimeError("Serial port/config not initialized on Instrument.")
#             self._serial = serial.Serial(
#                 port=port,
#                 baudrate=cfg.get("baudrate", 9600),
#                 bytesize=cfg.get("bytesize", 8),
#                 parity=cfg.get("parity", "N"),
#                 stopbits=cfg.get("stopbits", 1),
#                 timeout=cfg.get("timeout", 2),
#             )
#         if not self._serial.is_open:
#             self._serial.open()
#         try:
#             return cast(T, func(self, *args, **kwargs))
#         finally:
#             try:
#                 self._serial.close()
#             except Exception:
#                 pass
#     return wrapper


# class Instrument(ABC):
#     """Abstract base class for instruments providing common I/O, scheduling and file ops."""

#     # Class-level annotations (Pylance-friendly) for instance attributes
#     _serial: Optional[Any]
#     _serial_port: Optional[str]
#     _serial_cfg: Dict[str, Any]
#     _sockmode: str
#     _sockaddr: tuple[str, int]
#     _socktout: float
#     _socksleep: float
#     _averaging_interval: int
#     _reporting_interval: int
#     _staging_zip: bool
#     _file_timestamp_format: str
#     _header: str
#     _data: str
#     _saved_data_path: Optional[Path]

#     def __init__(self, name: str, config_path: str):
#         self._name = name
#         self._config_path = config_path
#         self._general = load_config(config_path)
#         self.logger = _get_logger(f"pydaq.{name}")

#         # Local paths
#         local = self._general.get("local", {})
#         root = Path(local.get("root", "."))
#         self.data_path = root / local.get("data", "data")
#         self.staging_path = root / local.get("staging", "staging")
#         ensure_dir(self.data_path)
#         ensure_dir(self.staging_path)

#         # Instrument section
#         self._params = self._general.get("instruments", {}).get(name, {})
#         self.simulate: bool = bool(self._params.get("simulate", self._general.get("simulate", False)))

#         # Metadata
#         try:
#             self._id = int(self._params.get("id", 0))
#         except Exception:
#             self._id = 0
#         self._serial_number: Optional[str] = self._params.get("serial_number")  # may be None

#         # Communication mode
#         self._params_comms = str(self._params.get("communication", "serial")).lower()

#         # Serial (lazy)
#         self._serial = None
#         self._serial_port = None
#         self._serial_cfg = {}
#         if self._params_comms == "serial":
#             port = self._params.get("serial", "COM1")
#             ports_cfg = self._general.get("ports", {})
#             port_cfg = ports_cfg.get(port, {"baudrate": 9600, "bytesize": 8, "parity": "N", "stopbits": 1, "timeout": 2})
#             self._serial_port = port
#             self._serial_cfg = dict(port_cfg)

#         # Socket
#         sock = self._params.get("socket", {})
#         self._sockmode = str(sock.get("mode", "tcp")).lower()
#         self._sockaddr = (sock.get("host", "127.0.0.1"), int(sock.get("port", 0)))
#         self._socktout = float(sock.get("timeout", 2))
#         self._socksleep = float(sock.get("sleep", 0.1))

#         # Scheduling / files
#         self._averaging_interval = int(self._params.get("averaging_interval", 1))   # minutes
#         self._reporting_interval = int(self._params.get("reporting_interval", 60))  # minutes
#         self._staging_zip = bool(self._params.get("staging_zip", True))
#         self._file_timestamp_format = "%Y%m%d%H"  # adjusted in setup_schedules()

#         # Data buffer and last saved path
#         self._header = f"# {self._name} S/N={self._serial_number} id={self._id}"
#         self._data = ""
#         self._saved_data_path = None

#         # Transfer config
#         self._transfer = self._general.get("transfer", {})

#     # ---------- abstract I/O ----------
#     @abstractmethod
#     def get_data(self) -> str: ...

#     @abstractmethod
#     def accumulate_data(self, data: str) -> None: ...

#     @abstractmethod
#     def _serial_comm(self, cmd: str) -> str: ...

#     @abstractmethod
#     def _socket_comm(self, cmd: str) -> str: ...

#     @abstractmethod
#     def set_datetime(self) -> None: ...

#     @abstractmethod
#     def get_config(self) -> dict: ...

#     @abstractmethod
#     def set_config(self) -> dict: ...

#     # ---------- file ops ----------
#     def save_data_file(self) -> None:
#         if not self._data:
#             self.logger.info("No data to save; skipping save_data_file().")
#             return
#         fname = f"{self._name}-{datetime.now().strftime(self._file_timestamp_format)}.dat"
#         target = self.data_path / fname
#         content = self._header + "\n" + self._data.rstrip("\n") + "\n"
#         target.write_text(content, encoding="utf-8")
#         self._saved_data_path = target
#         self.logger.info("Saved data file: %s", target)
#         self._data = ""

#     def stage_data_file(self) -> None:
#         if not self._saved_data_path or not self._saved_data_path.exists():
#             self.logger.warning("No saved data file to stage.")
#             return
#         src = self._saved_data_path
#         dst = self.staging_path / src.name

#         if self._staging_zip:
#             zip_dst = dst.with_suffix(".zip")
#             with zipfile.ZipFile(zip_dst, "w") as zf:
#                 zf.write(src, arcname=src.name)
#             self.logger.info("Staged %s → %s", src, zip_dst)
#         else:
#             dst.write_bytes(src.read_bytes())
#             self.logger.info("Staged %s → %s", src, dst)

#     # ---------- transfer (SFTP adapter) ----------
#     def transfer_files(self) -> bool:
#         sftp_cfg = self._transfer.get("sftp", {})
#         if not sftp_cfg or not sftp_cfg.get("host"):
#             self.logger.info("No SFTP config found; skipping transfer.")
#             return False
#         try:
#             from utils.sftp import SFTPClient  # type: ignore
#         except Exception as err:
#             self.logger.error("SFTP not available: %s", err)
#             return False

#         cfg = {
#             "host": sftp_cfg.get("host"),
#             "usr": sftp_cfg.get("usr") or sftp_cfg.get("user"),
#             "key_path": sftp_cfg.get("key_path") or sftp_cfg.get("key"),
#             "passphrase": sftp_cfg.get("passphrase"),
#             "password": sftp_cfg.get("password"),
#             "staging": str(self.staging_path),
#             "remote": sftp_cfg.get("remote") or sftp_cfg.get("remote_path"),
#             "accept_unknown_host_keys": sftp_cfg.get("accept_unknown_host_keys", True),
#             "timeouts": sftp_cfg.get("timeouts", {}),
#         }
#         try:
#             client = SFTPClient(config=cfg, logger=self.logger)
#             client.transfer_files(remove_on_success=True)
#             return True
#         except Exception as err:
#             self.logger.error("SFTP transfer failed: %s", err)
#             return False

#     # ---------- scheduling ----------
#     def setup_schedules(self) -> bool:
#         try:
#             schedule.every(self._averaging_interval).minutes.do(self.get_data)
#             schedule.every(self._averaging_interval).minutes.do(self.save_data_file)

#             if self._reporting_interval == 10:
#                 self._file_timestamp_format = "%Y%m%d%H%M"
#                 for m in (0, 10, 20, 30, 40, 50):
#                     schedule.every().hour.at(f":{m:02}").do(self.stage_data_file)
#             elif self._reporting_interval >= 1440:
#                 self._file_timestamp_format = "%Y%m%d"
#                 schedule.every().day.at("00:00").do(self.stage_data_file)
#             else:
#                 self._file_timestamp_format = "%Y%m%d%H"
#                 schedule.every().hour.at(":00").do(self.stage_data_file)
#             return True
#         except Exception as err:
#             self.logger.error("setup_schedules failed: %s", err)
#             return False
        
# """
# Abstract instrument base class for implementing data acquisition drivers.

# Each concrete instrument implementation should subclass this and define methods
# for reading, accumulating, and writing data. Configuration is loaded from a
# YAML file using utilities provided in `utils.config_utils`.
# """

# import logging
# import random  # Used for simulation purposes in tests
# import zipfile
# from abc import ABC, abstractmethod
# from datetime import datetime
# from importlib import import_module
# from pathlib import Path
# from typing import Optional

# import schedule
# import serial

# from utils.config import load_config

# class Instrument(ABC):
#     """
#     Abstract base class for instruments.
#     Specific instrument classes should implement the required methods.
#     Interfaces can use TCP/IP or serial ports.
#     """

#     def __init__(self, name: str, config_path: str):
#         """
#         Initialize an instrument with a name and configuration loaded from a YAML file.

#         Args:
#             name (str): Name of the instrument.
#             config_path (str): Path to the YAML configuration file.
#         """
#         self._name = name
#         self.logger = logging.getLogger(f"Instrument:{self._name}")

#         # Load general and instrument configuration from the provided YAML file.
#         self._general = load_config(config_path)
#         if not self._general:
#             raise ValueError(f"Configuration file {config_path} not found or invalid.")

#         self._params = self._general.get("instruments", {}).get(name, {})
#         if not self._params:
#             raise ValueError(f"Instrument '{name}' not found in configuration.")

#         # Configure local paths
#         self.data_path = (
#             Path(self._general.get("local", {}).get("root", {}).get("data", "data")) / self._name
#             ).expanduser().resolve()
#         self.data_path.mkdir(parents=True, exist_ok=True)

#         self.staging_path = (
#             Path(self._general.get("local", {}).get("root", {}).get("staging", "staging")) / self._name
#             ).expanduser().resolve()
#         self.staging_path.mkdir(parents=True, exist_ok=True)
#         self._staging_zip = self._params.get("staging_zip", True)

#         # Instrument metadata
#         self._id = self._params.get("id", "ID unknown")
#         self._serial_number = self._params.get("serial_number", "S/N unknown")
#         self.simulate = self._params.get("simulate", False)

#         # Configure instrument communication
#         self._params_comms = self._params.get("communication", "").lower()
#         if not self._params_comms in ("serial", "socket"):
#             raise ValueError("Instrument communication must be 'serial' or 'socket'. Verify config.")

#         if self._params_comms=="serial":
#             port = self._params.get("serial", "COM1")
#             port_cfg = self._general.get("ports", {}).get(port, {
#                 "baudrate": 9600,
#                 "bytesize": 8,
#                 "parity": "N",
#                 "stopbits": 1,
#                 "timeout": 2    # seconds
#             })
#             self._serial = serial.Serial(
#                 port=port,
#                 baudrate=port_cfg["baudrate"],
#                 bytesize=port_cfg["bytesize"],
#                 parity=port_cfg["parity"],
#                 stopbits=port_cfg["stopbits"],
#                 timeout=port_cfg["timeout"]
#             )
#             if self._serial.is_open:
#                 self._serial.close()
#         else:
#             sock = self._params["socket"]
#             self._sockmode = sock.get("mode", "tcp").lower()
#             self._sockaddr = (sock["host"], sock["port"])
#             self._socktout = sock["timeout"]
#             self._socksleep = sock["sleep"]

#         # Configure data logging
#         self._data: str = ""
#         self._saved_data_path: Optional[Path] = None
#         self._header: str = ""
#         self._averaging_interval = self._params.get("averaging_interval", 1)
#         self._reporting_interval = self._params.get("reporting_interval", 60)
#         if self._reporting_interval >= 1440:
#             self._file_timestamp_format = "%Y%m%d"
#         elif self._reporting_interval >= 60:
#             self._file_timestamp_format = "%Y%m%d%H"
#         else:
#             self._file_timestamp_format = "%Y%m%d%H%M"

#         # Configure data transfer
#         self._remote_protocol = self._general.get("remote", {}).get("transfer", "").lower()
#         if self._remote_protocol == "sftp":
#             self.remote_path = Path(self._general.get("remote", {}).get("sftp", {}).get("usr", ""))
#         elif self._remote_protocol == "mqtt":
#             self.remote_path = Path(self._general.get("remote", {}).get("mqtt", {}).get("topic", ""))
#         else:
#             self.remote_path = Path()


#     @property
#     def name(self) -> str:
#         """Return the instrument name."""
#         return self._name

#     @property
#     def instrument_config(self) -> dict:
#         """Return the instrument-specific configuration dictionary."""
#         return self._params


#     def send_command(self, cmd: str) -> str:
#         """
#         Send a command and receive the response (if any) via serial or TCP/IP.

#         Args:
#             cmd (str): Command string to send.

#         Returns:
#             str: Decoded response string.
#         """
#         try:
#             if self.simulate:
#                 return str(random.randint(0, 1000) / 10)  # Simulate a random response
#             if self._params_comms=="serial":
#                 return self._serial_comm(cmd)
#             else:
#                 return self._socket_comm(cmd)
#         except Exception as err:
#             self.logger.error(f"Communication error: {err}")
#             return ""


#     def save_data_file(self) -> None:
#         """
#         Save accumulated measurement data to a timestamped .dat file.

#         The file is written to `self.data_path`, with a header if it does not yet exist.
#         The file name includes the instrument name and a formatted timestamp.
#         """
#         try:
#             if not self._data.strip():
#                 self.logger.warning("No data to save.")
#                 return

#             timestamp = datetime.now().strftime(self._file_timestamp_format)
#             filename = f"{self._name}-{timestamp}.dat"
#             data_file_path = self.data_path / filename

#             # Write data to file (append if it exists)
#             mode = "a" if data_file_path.exists() else "w"
#             with open(data_file_path, mode) as fh:
#                 if mode == "w":
#                     fh.write(self._header + "\n")
#                 fh.write(self._data)

#             self.logger.info(f"Saved data to {data_file_path}")
#             self._saved_data_path = data_file_path

#             # Clear the accumulated data after saving
#             self._data = ""

#         except Exception as err:
#             self.logger.error("Failed to save data: %s", err)


#     def stage_data_file(self) -> None:
#         """
#         Stage the most recently saved data file to the staging directory.

#         The staging path is derived from the config. If it does not exist, it is created.
#         """
#         try:
#             if not self._saved_data_path or not self._saved_data_path.exists():
#                 self.logger.warning("No saved data file to stage.")
#                 return

#             source = self._saved_data_path
#             target = self.staging_path / self._saved_data_path.name

#             if source.exists():
#                 if self._staging_zip:
#                     with zipfile.ZipFile(target.with_suffix('.zip'), 'w') as zf:
#                         zf.write(source, arcname=source.name)
#                     self.logger.info(f"Staged {source} to {target.with_suffix('.zip')}")
#                 else:
#                     # Copy the file directly if not zipping
#                     target.write_bytes(source.read_bytes())
#                 self.logger.info(f"Staged {source} → {target}")
#             else:
#                 self.logger.warning(f"No data file found to stage: {source}")

#         except Exception as err:
#             self.logger.error("Failed to stage data file: %s", err)


#     def transfer_files(self) -> bool:
#         """
#         Transfer staged data using the globally configured transfer protocol.

#         Returns:
#             bool: True if transfer succeeded, False otherwise.
#         """
#         transfer = self._general.get("transfer", "").lower()

#         if transfer == "sftp":
#             try:
#                 sftp = import_module("utils.sftp")
#                 client = sftp.SFTPClient(self._general)

#                 client.transfer_files(
#                     local_path=str(self.staging_path),
#                     remote_path=self._general["sftp"]["remote_path"],
#                     remove_on_success=True
#                 )
#                 return True
#             except Exception as err:
#                 self.logger.error(f"SFTP transfer failed: {err}")
#                 return False
#         elif transfer == "mqtt":
#             try:
#                 mqtt = import_module("utils.mqtt")
#                 instr_topic = self._params.get("mqtt_topic")
#                 if not instr_topic:
#                     raise ValueError("Missing 'mqtt_topic' in instrument config.")

#                 client = mqtt.MQTTClient(self._general, instr_topic)
#                 client.transfer_files(
#                     local_path=str(self.staging_path),
#                     remove_on_success=True
#                 )
#                 return True
#             except Exception as err:
#                 self.logger.error(f"MQTT transfer failed: {err}")
#                 return False

#         else:
#             self.logger.error(f"Transfer transfer '{transfer}' is not supported.")
#             return False


#     def setup_schedules(self) -> bool:
#         """
#         Set up acquisition and file staging intervals using `schedule`.

#         Returns:
#             bool: True if schedules are successfully set.
#         """
#         try:
#             schedule.every(self._averaging_interval).minutes.at(":00").do(self.get_data)
#             schedule.every(self._averaging_interval).minutes.at(":00").do(self.save_data_file)

#             if self._reporting_interval == 10:
#                 self._file_timestamp_format = "%Y%m%d%H%M"
#                 for minute in ["00", "10", "20", "30", "40", "50"]:
#                     schedule.every().hour.at(f"{minute}:01").do(self.stage_data_file)
#             elif self._reporting_interval == 1440:
#                 self._file_timestamp_format = "%Y%m%d"
#                 schedule.every().day.at("00:00:01").do(self.stage_data_file)
#             else:
#                 self._file_timestamp_format = "%Y%m%d%H"
#                 schedule.every().hour.at("00:01").do(self.stage_data_file)

#             return True
#         except Exception as err:
#             self.logger.error(err)
#             return False


#     @abstractmethod
#     def get_data(self) -> str:
#         """
#         Read (instant) measurement data from the instrument.

#         Returns:
#             str: Measurement data from instrument.
#         """
#         pass

#     @abstractmethod
#     def accumulate_data(self, data: str):
#         """
#         Accumulate data from multiple reads into internal buffer.

#         Args:
#             data (str): Data string to accumulate.
#         """
#         pass

#     @abstractmethod
#     def _serial_comm(self, cmd: str) -> str:
#         """
#         Send a command over a serial connection.

#         Args:
#             cmd (str): Command string to send.

#         Returns:
#             str: Response from the instrument.
#         """
#         pass

#     @abstractmethod
#     def _socket_comm(self, cmd: str) -> str:
#         """
#         Send a command over a socket connection.

#         Args:
#             cmd (str): Command string to send.

#         Returns:
#             str: Response from the instrument.
#         """
#         pass

#     @abstractmethod
#     def set_datetime(self):
#         """
#         Set the instrument's internal clock to the current system date and time.
#         """
#         pass

#     @abstractmethod
#     def get_config(self) -> dict:
#         """
#         Retrieve the instrument's current configuration.

#         Returns:
#             dict: Key-value pairs of configuration parameters.
#         """
#         pass

#     @abstractmethod
#     def set_config(self) -> dict:
#         """
#         Set the instrument's configuration from internal settings.

#         Returns:
#             dict: Key-value pairs of set commands and their responses.
#         """
#         pass
