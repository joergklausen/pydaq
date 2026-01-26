from __future__ import annotations

import logging
import socket
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Tuple

import numpy as np
import colorama

from ..instrument import Instrument, with_serial
from .acoem_proto import AcoemClient
from .aurora_proto import AuroraClient


class NEPH(Instrument):
    """
    Unified pydaq driver for ACOEM NE-300 / Ecotech Aurora 3000 nephelometers.

    It wires the abstract :class:`Instrument` base-class to a protocol
    implementation:

    - ``protocol: "acoem"``  → :class:`AcoemClient` (binary ACOEM protocol)
    - ``protocol: "aurora"`` → :class:`AuroraClient` (Aurora VI/`***D` protocol)

    Config (per-instrument section in your YAML) is expected to contain at least:

    .. code-block:: yaml

        instruments:
          neph:
            communication: socket | serial
            socket:
              host: 192.168.0.200
              port: 3602
              timeout: 2
              sleep: 0.1
            protocol: acoem | aurora
            serial_id: 1              # ACOEM SID / Aurora station ID
            data_log:
              parameters: [ ... ]     # optional, ACOEM only
              interval: 60            # seconds, optional

    """

    def __init__(self, config_path: str, name: str = "NEPH") -> None:
        super().__init__(name=name, config_path=config_path)

        # Per-driver params (Instrument already puts the instrument block here)
        params: Dict[str, Any] = self._params if isinstance(self._params, dict) else {}
        self._driver_params: Dict[str, Any] = params

        # Protocol detection
        model = str(params.get("model", "")).strip().lower()
        protocol = str(params.get("protocol", "")).strip().lower()

        if not protocol:
            # Heuristic: default to ACOEM unless clearly Aurora
            if model in {"aurora", "aurora3000", "aurora 3000", "a3000"}:
                protocol = "aurora"
            else:
                protocol = "acoem"

        if protocol not in {"acoem", "aurora"}:
            raise ValueError(f"[{self._name}] Unsupported protocol '{protocol}'")

        self._protocol = protocol

        # Aurora / ACOEM station ID (SID)
        try:
            self._serial_id = int(params.get("serial_id", 1))
        except Exception:
            self._serial_id = 1

        # Optional data-logger hints from config (ACOEM)
        data_log_cfg = params.get("data_log", {}) or {}
        self._data_log_params: List[int] = [
            int(p) for p in data_log_cfg.get("parameters", []) if p is not None
        ]

        # Header & file-extension
        if self._protocol == "aurora":
            default_header = (
                "dtm,"
                "ssp1,ssp2,ssp3,"
                "sbsp1,sbsp2,sbsp3,"
                "sample_temp,enclosure_temp,RH,pressure,"
                "major_state,DIO_state"
            )
        else:
            # NE-300 typical layout; adjust if you have a different mapping
            default_header = (
                "dtm,"
                "fs_b,fs_g,fs_r,"
                "bs_b,bs_g,bs_r,"
                "sample_temp,enclosure_temp,RH,pressure,state"
            )
        header = params.get("header")
        self._header = str(header or default_header)
        self._filename_extension = "csv"

        # Protocol implementation
        if self._protocol == "aurora":
            self._impl = AuroraClient(self, params)
        else:
            self._impl = AcoemClient(self, params)

        self.logger.info(
            "[%s] NEPH initialised (protocol=%s, serial_id=%s, communication=%s)",
            self._name,
            self._protocol,
            self._serial_id,
            self._params_comms,
        )

    # ------------------------------------------------------------------
    # Low-level transport
    # ------------------------------------------------------------------

    @with_serial
    def _serial_comm(self, cmd: str) -> str:  # type: ignore[override]
        """
        Send a line-oriented ASCII command over serial and return its response.

        The serial port is opened/closed by the :func:`with_serial` decorator.
        """
        # NOTE: with_serial is responsible for constructing `self._serial`
        ser = self._serial  # type: ignore[assignment]
        assert ser is not None  # noqa: S101 - runtime assertion is fine here

        try:
            # Clear buffers if supported
            try:
                ser.reset_input_buffer()
                ser.reset_output_buffer()
            except Exception:
                pass

            wire = (cmd + "\r").encode("ascii", "ignore")
            ser.write(wire)
            # Small pause to allow instrument to respond
            time.sleep(self._socksleep or 0.1)

            buf = bytearray()
            timeout = float(self._serial_cfg.get("timeout", 2))
            deadline = time.time() + timeout

            while time.time() < deadline:
                n_waiting = getattr(ser, "in_waiting", 0)
                if n_waiting:
                    buf += ser.read(n_waiting)
                else:
                    time.sleep(0.02)

            text = buf.decode("utf-8", errors="replace")
            # Normalise Aurora-style line endings and separators
            return text.replace("\r\n\n", "\r\n").strip()
        except Exception as err:
            self.logger.error(
                "%sSerial I/O failed on %s: %s%s",
                colorama.Fore.RED,
                getattr(self, "_serial_port", "<?>"),
                err,
                colorama.Fore.GREEN,
            )
            raise

    def _tcp_request(self, message: bytes, expect_response: bool = True, verbosity: int = 0) -> bytes:
        """
        Send a raw TCP message and optionally read back the reply.

        For ACOEM, this is used for the full binary protocol. For Aurora,
        it is used for VI/`***D` ASCII commands when `communication: socket`.
        """
        if self._sockmode != "tcp":
            raise RuntimeError(f"[{self._name}] _tcp_request requires sock mode 'tcp'.")

        host, port = self._sockaddr
        if not port:
            raise RuntimeError(f"[{self._name}] socket host/port not configured.")

        rcvd = b""
        with self._io_lock:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(self._socktout)
                    s.connect(self._sockaddr)

                    if self._socksleep:
                        time.sleep(self._socksleep)

                    start = time.perf_counter()
                    if verbosity > 0 and self.logger.isEnabledFor(logging.DEBUG):
                        self.logger.debug("[%s] TCP send: %r", self._name, message)
                    s.sendall(message)

                    if expect_response:
                        if self._protocol == "acoem":
                            # Read until EOT (0x04)
                            while b"\x04" not in rcvd:
                                chunk = s.recv(1024)
                                if not chunk:
                                    break
                                rcvd += chunk
                        elif self._protocol == "aurora":
                            # Read until CRLF or CRLFCRLF
                            while not (rcvd.endswith(b"\r\n") or rcvd.endswith(b"\r\n\n")):
                                chunk = s.recv(1024)
                                if not chunk:
                                    break
                                rcvd += chunk
                        else:
                            raise ValueError(f"[{self._name}] Unknown protocol '{self._protocol}'")

                        rcvd = rcvd.strip()
                        # Remove potential telnet preamble
                        rcvd = rcvd.replace(b"\xff\xfb\x01\xff\xfe\x01\xff\xfb\x03", b"")

                        if verbosity > 1 and self.logger.isEnabledFor(logging.DEBUG):
                            elapsed = time.perf_counter() - start
                            self.logger.debug(
                                "[%s] TCP recv (%d B, %.3fs): %r",
                                self._name,
                                len(rcvd),
                                elapsed,
                                rcvd,
                            )
            except Exception as err:
                self.logger.error(
                    "%sSocket I/O failed to %s:%s: %s%s",
                    colorama.Fore.RED,
                    host,
                    port,
                    err,
                    colorama.Fore.GREEN,
                )
                raise
        return rcvd

    def _socket_comm(self, cmd: str) -> str:
        """
        Send an ASCII command over the configured socket and return the decoded reply.
        """
        wire = (cmd + "\r").encode("ascii", "ignore")
        raw = self._tcp_request(wire, expect_response=True)
        return raw.decode("utf-8", errors="replace").replace("\r\n\n", "\r\n").strip()

    def _use_serial(self) -> bool:
        """Return True if this instrument is configured to use serial comms."""
        return self._params_comms == "serial"

    # ------------------------------------------------------------------
    # High-level delegations into protocol implementations
    # ------------------------------------------------------------------

    def get_values(self, parameters: Iterable[int], verbosity: int = 0) -> Dict[int, Any]:
        return self._impl.get_values(list(parameters), verbosity=verbosity)

    def set_value(self, parameter_id: int, value: int, verify: bool = True, verbosity: int = 0) -> int:
        return self._impl.set_value(parameter_id, value, verbosity=verbosity)

    def get_datetime(self, verbosity: int = 0) -> datetime:
        return self._impl.get_datetime(verbosity=verbosity)

    # Instrument API: set_datetime must exist, even if protocol can't do it.
    def set_datetime(self) -> None:  # type: ignore[override]
        try:
            self._impl.set_datetime(datetime.now(), verbosity=0)
        except NotImplementedError:
            self.logger.info(
                "[%s] set_datetime not supported for protocol '%s'",
                self._name,
                self._protocol,
            )

    def get_id(self, verbosity: int = 0) -> Dict[str, str]:
        return self._impl.get_id(verbosity=verbosity)

    def get_current_operation(self, verbosity: int = 0) -> int:
        return self._impl.get_current_operation(verbosity=verbosity)

    def set_current_operation(self, state: int = 0, verify: bool = True, verbosity: int = 0) -> int:
        return self._impl.set_current_operation(state=state, verify=verify, verbosity=verbosity)

    def get_logged_data(
        self,
        start: datetime,
        end: datetime | None = None,
        verbosity: int = 0,
    ) -> List[Dict[str | int, Any]]:
        """Retrieve logged data from the instrument's internal data logger (if supported).

        For ACOEM, this uses the binary ACOEM ``Get Logged Data`` command (A.3.8)
        over the timestamp range [start, end].

        For Aurora, this uses the ``***R``/``***D`` logger commands and ignores
        start / end (the instrument decides what it returns).
        """
        if end is None:
            # Some protocol implementations expect a concrete end timestamp.
            end = start
        return self._impl.get_logged_data(start=start, end=end, verbosity=verbosity)

    def logged_data_to_csv(self, records: List[Dict[str | int, Any]], sep: str = ",") -> str:
        """Convert logged-data records (as returned by :meth:`get_logged_data`) to CSV."""
        return self._impl.logged_data_to_csv(records, sep=sep)

    # ------------------------------------------------------------------
    # Implement abstract Instrument API
    # ------------------------------------------------------------------

    def accumulate_data(self, data: str) -> None:
        """Append a line of text data to the internal buffer."""
        if not data:
            return
        if not data.endswith("\n"):
            data += "\n"
        with self._buf_lock:
            self._data += data

    def get_current_data(self, sep: str = ",") -> str:
        """Retrieve a single line of current (or latest logged) data."""
        return self._impl.get_current_data(sep=sep)

    def parse_current_data(self, reading: str) -> Tuple[datetime, np.ndarray]:
        """Parse a single CSV reading into (datetime, values)."""
        return self._impl.parse_current_data(reading)

    def get_data(self) -> str:  # type: ignore[override]
        """
        Acquire one line of data and accumulate it in the internal buffer.

        Returns the line as a string so callers can also use it interactively.
        """
        line = self.get_current_data(sep=",")
        if line:
            self.accumulate_data(line)
        return line

    def get_config(self) -> dict:  # type: ignore[override]
        """
        Return a snapshot of relevant configuration from the instrument.

        This calls protocol-specific helpers (e.g. data-logger config, intervals, etc.)
        and merges them with basic identity info.
        """
        cfg: Dict[str, Any] = {}
        try:
            ident = self.get_id()
            if ident:
                cfg.update(ident)
        except Exception:
            pass
        try:
            cfg.update(self._impl.get_config_snapshot())
        except AttributeError:
            pass
        return cfg

    def set_config(self) -> dict:  # type: ignore[override]
        """
        Apply protocol-specific configuration to the instrument.

        For ACOEM this typically configures data-logger parameters. For Aurora this
        currently returns an empty mapping.
        """
        try:
            return self._impl.set_config_snapshot()
        except AttributeError:
            return {}

    def display_data(self) -> None:  # type: ignore[override]
        """Acquire one line of data and log it at INFO level."""
        try:
            line = self.get_data()
            if line:
                self.logger.info("[%s] %s", self._name, line)
        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)


class NE300(NEPH):
    """
    Convenience wrapper for an NE-300 operated with the ACOEM binary protocol.

    YAML example:

    .. code-block:: yaml

        instruments:
          ne300:
            driver: instr.ecotech.NE300
            communication: socket
            socket:
              host: 192.168.0.200
              port: 3602
            protocol: acoem   # optional; forced by this wrapper
    """

    def __init__(self, config_path: str, name: str = "NE300") -> None:
        super().__init__(config_path=config_path, name=name)
        # Force ACOEM protocol / implementation regardless of config
        self._protocol = "acoem"
        self._impl = AcoemClient(self, self._driver_params)


class Aurora3000(NEPH):
    """
    Convenience wrapper for an Aurora 3000 using the Aurora VI/`***D` protocol.
    """

    def __init__(self, config_path: str, name: str = "Aurora3000") -> None:
        super().__init__(config_path=config_path, name=name)
        # Force Aurora protocol / implementation regardless of config
        self._protocol = "aurora"
        self._impl = AuroraClient(self, self._driver_params)
