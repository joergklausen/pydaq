"""
Ecotech / ACOEM nephelometer drivers built on the shared Instrument base.

This module provides a single driver class :class:`NEPH` that can speak either

- the full **ACOEM binary protocol** used by the NE-300, or
- the classic **Aurora 3000 "VIxx" serial protocol**.

The protocol is selected from the YAML config via ``model`` (and optionally
``protocol``) in the instrument's ``params`` section.

Example YAML
------------

instruments:
  nrb_neph:
    driver: ecotech.NEPH
    params:
      model: NE300          # or Aurora3000 / Aurora / A3000
      protocol: acoem       # optional; inferred from model if omitted
      serial_id: 1          # station/instrument id used in VIxx commands
      communication: socket # or "serial"
      socket:
        host: 192.168.0.200
        port: 5025
        timeout: 5
      header: "dtm,..."
"""

from __future__ import annotations

import logging
import socket
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple

import colorama
import numpy as np

from instrument import Instrument, with_serial


def _iso(ts: datetime) -> str:
    """Return ISO 8601 string without microseconds."""
    return ts.replace(microsecond=0).isoformat(sep=" ")


class NEPH(Instrument):
    """
    Unified nephelometer driver (Acoem NE-300 / Ecotech Aurora 3000).

    High-level API
    --------------
    The following methods are available for *both* protocols where meaningful,
    falling back to ``NotImplementedError`` where a feature is protocol-specific:

    - :meth:`get_values`           – query one or more parameters by ID
    - :meth:`set_value`            – set a parameter by ID
    - :meth:`get_datetime` / :meth:`set_datetime`
    - :meth:`get_id`               – human-readable identification
    - :meth:`get_version`          – firmware version (ACOEM only)
    - :meth:`get_current_operation` / :meth:`set_current_operation`
    - :meth:`get_data_log_config`, :meth:`set_datalog_interval`
    - :meth:`get_logged_data`      – download logged data (ACOEM only, stub here)

    In addition, :meth:`get_data` implements the abstract method from
    :class:`Instrument` and returns one CSV line that is also appended to the
    internal text buffer via :meth:`accumulate_data`.
    """

    # Parameter IDs used by get_current_data() for ACOEM protocol
    _ACOEM_CURRENT_PARAMS: Tuple[int, ...] = (
        1,        # timestamp
        1635000,  # fs_b
        1525000,  # fs_g
        1450000,  # fs_r
        1635090,  # bs_b
        1525090,  # bs_g
        1450090,  # bs_r
        5001,     # sample temperature
        5004,     # enclosure temperature
        5003,     # RH
        5002,     # pressure
        4036,     # valve status / state
        4035,     # operation state
    )

    def __init__(self, config_path: str, name: str = "NEPH") -> None:
        super().__init__(name=name, config_path=config_path)

        params = self._params if isinstance(self._params, dict) else {}

        # Determine protocol from explicit "protocol" or from model.
        model = str(params.get("model", "")).strip().lower()
        protocol = str(params.get("protocol", "")).strip().lower()
        if protocol not in {"acoem", "aurora"}:
            if model in {"aurora3000", "aurora 3000", "aurora", "a3000"}:
                protocol = "aurora"
            else:
                protocol = "acoem"
        self._protocol: str = protocol

        # Serial-id used in VIxx commands on Aurora; optional for NE-300.
        try:
            self.serial_id: int = int(params.get("serial_id", 1))
        except Exception:
            self.serial_id = 1

        # Header & file extension – override Instrument defaults.
        if self._protocol == "aurora":
            default_header = (
                "dtm,ssp1,ssp2,ssp3,sbsp1,sbsp2,sbsp3,"
                "sample_temp,enclosure_temp,RH,pressure,major_state,DIO_state"
            )
        else:
            default_header = "dtm,fs_b,fs_g,fs_r,bs_b,bs_g,bs_r,T,RH,P,state"

        header = params.get("header")
        self._header = str(header or default_header)
        self._filename_extension = "csv"

        # ACOEM binary protocol state
        self._tcpip_line_is_busy: bool = False

        self.logger.info(
            "[%s] NEPH initialised (protocol=%s, serial_id=%s, communication=%s)",
            self._name,
            self._protocol,
            self.serial_id,
            self._params_comms,
        )

    # ------------------------------------------------------------------
    # Low-level communication primitives
    # ------------------------------------------------------------------

    @with_serial
    def _serial_comm(self, cmd: str) -> str:  # type: ignore[override]
        """Send a command over the configured serial port and return the reply."""
        assert self._serial is not None
        ser = self._serial
        try:
            try:
                ser.reset_input_buffer()
                ser.reset_output_buffer()
            except Exception:
                # Best-effort; not fatal if reset fails.
                pass

            ser.write((cmd + "\r").encode("ascii", errors="ignore"))
            time.sleep(self._socksleep or 0.1)

            buf = bytearray()
            deadline = time.time() + float(self._serial_cfg.get("timeout", 2))
            while time.time() < deadline:
                n = getattr(ser, "in_waiting", 0)
                if n:
                    buf += ser.read(n)
                else:
                    time.sleep(0.02)

            out = buf.decode("utf-8", errors="replace")
            # Normalise slightly quirky line endings that Aurora emits.
            return out.replace("\r\n\n", "\r\n").strip()
        except Exception as err:
            self.logger.exception(
                "Serial I/O failed",
                extra={
                    "to_logfile": True,
                    "instrument": self._name,
                    "cmd": cmd,
                    "port": str(self._serial_cfg.get("port")),
                },
            )
            raise

    def _socket_comm(self, cmd: str) -> str:  # type: ignore[override]
        """
        Simple string-based socket communication.

        This is primarily used for Aurora-style ASCII commands; the full ACOEM
        binary protocol is implemented in :meth:`_tcpip_comm`.
        """
        wire = (cmd + "\r").encode("ascii", errors="ignore")
        reply = self._tcpip_comm(wire, expect_response=True)
        return reply.decode("utf-8", errors="replace").replace("\r\n\n", "\r\n").strip()

    def _tcpip_comm(self, message: bytes, expect_response: bool = True, verbosity: int = 0) -> bytes:
        """
        Send and receive data using the full ACOEM/Aurora TCP protocol.

        This is a near verbatim port of the older :mod:`acoem` NEPH driver,
        adapted to use the socket configuration provided by :class:`Instrument`.
        """
        if self._sockmode != "tcp":
            raise RuntimeError("NEPH: only TCP socket mode ('mode: tcp') is supported for _tcpip_comm().")

        host, port = self._sockaddr
        if not port:
            raise RuntimeError("NEPH: socket host/port not configured.")

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
                        self.logger.debug("Socket message sent: %r", message)
                    s.sendall(message)

                    if expect_response:
                        if self._protocol == "acoem":
                            # ACOEM: read until End-of-Transmission 0x04
                            while b"\x04" not in rcvd:
                                chunk = s.recv(1024)
                                if not chunk:
                                    break
                                rcvd += chunk
                        elif self._protocol == "aurora":
                            # Aurora: read until CRLF / CRLFCRLF
                            while not (rcvd.endswith(b"\r\n") or rcvd.endswith(b"\r\n\n")):
                                chunk = s.recv(1024)
                                if not chunk:
                                    break
                                rcvd += chunk
                        else:
                            raise ValueError("Protocol not recognised for _tcpip_comm().")

                        rcvd = rcvd.strip()
                        # Strip possible telnet negotiation preamble
                        rcvd = rcvd.replace(b"\xff\xfb\x01\xff\xfe\x01\xff\xfb\x03", b"")

                        end = time.perf_counter()
                        if verbosity > 1 and self.logger.isEnabledFor(logging.DEBUG):
                            self.logger.debug(
                                "Socket response (%d bytes, %.3fs): %r", len(rcvd), end - start, rcvd
                            )

            except Exception as err:
                self.logger.exception(
                    "Socket I/O failed",
                    extra={
                        "to_logfile": True,
                        "instrument": self._name,
                        "sockaddr": f"{host}:{port}",
                    },
                )
                raise

        return rcvd

    # ------------------------------------------------------------------
    # ACOEM binary protocol helpers (ported from acoem.NEPH)
    # ------------------------------------------------------------------

    def _acoem_checksum(self, payload: bytes) -> int:
        """Return simple checksum used by ACOEM packets."""
        return sum(payload) & 0xFF

    def _acoem_construct_message(self, command: int, parameter_id: int = 0, payload: bytes = b"") -> bytes:
        """
        Construct an ACOEM packet.

        Layout (see ACOEM manual):

        Byte |  1 | 2  | 3  | 4  | 5..6    | 7..10   | 11       | 12
             | STX| SID| CMD| ETX| msg_len | msg_data| checksum | EOT
        """
        try:
            stx = 0x02
            etx = 0x03
            eot = 0x04
            sid = int(self.serial_id)

            # Message data: optional parameter-id prefix + payload
            msg_data = b""
            if parameter_id:
                msg_data += parameter_id.to_bytes(2, byteorder="big")
            if payload:
                msg_data += payload

            msg_len = len(msg_data).to_bytes(2, byteorder="big")
            core = bytes([stx, sid, command, etx]) + msg_len + msg_data
            csum = self._acoem_checksum(core).to_bytes(1, byteorder="big")

            return core + csum + bytes([eot])
        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)
            return b""

    def _acoem_bytes2int(self, response: bytes, verbosity: int = 0) -> List[int]:
        """Convert ACOEM payload bytes (after header) into big-endian integers."""
        try:
            # Strip header/footer if still present (STX..EOT).
            if len(response) >= 8 and response[0] == 0x02:
                # STX SID CMD ETX len_hi len_lo  ...  checksum EOT
                msg_len = int.from_bytes(response[4:6], byteorder="big")
                msg_data = response[6 : 6 + msg_len]
            else:
                msg_data = response

            ints: List[int] = []
            # Interpret as 4-byte big-endian integers
            for i in range(0, len(msg_data), 4):
                chunk = msg_data[i : i + 4]
                if len(chunk) == 4:
                    ints.append(int.from_bytes(chunk, byteorder="big", signed=False))

            if verbosity > 1 and self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug("ACOEM bytes→int: %s -> %s", response, ints)

            return ints
        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)
            return []

    @staticmethod
    def _acoem_timestamp_to_datetime(ts: int) -> datetime:
        """
        Convert ACOEM timestamp (binary date/time field) to :class:`datetime`.

        The encoding is described in the ACOEM manual; this implementation is
        a direct port of the legacy NEPH driver.
        """
        # Bits: yyyy(6) mm(4) dd(5) HH(5) MM(6) SS(6)
        bits = f"{ts:032b}"[-32:]  # safeguard
        yyyy = int(bits[0:6], 2) + 2000
        mm = int(bits[6:10], 2)
        dd = int(bits[10:15], 2)
        HH = int(bits[15:20], 2)
        MM = int(bits[20:26], 2)
        SS = int(bits[26:32], 2)
        return datetime(yyyy, mm, dd, HH, MM, SS)

    @staticmethod
    def _acoem_datetime_to_timestamp(dtm: datetime) -> bytes:
        """Inverse of :meth:`_acoem_timestamp_to_datetime`."""
        HH = format(dtm.time().hour, "05b")
        MM = format(dtm.time().minute, "06b")
        SS = format(dtm.time().second, "06b")
        dd = format(dtm.date().day, "05b")
        mm = format(dtm.date().month, "04b")
        yyyy = format(dtm.date().year - 2000, "06b")
        val = int(yyyy + mm + dd + HH + MM + SS, 2)
        return val.to_bytes(4, byteorder="big")

    # ------------------------------------------------------------------
    # High-level protocol operations (subset of legacy NEPH API)
    # ------------------------------------------------------------------

    def get_values(self, parameters: List[int], verbosity: int = 0) -> Dict[int, Any]:
        """
        Request the value of one or more instrument parameters.

        For ACOEM, this uses the binary *Get Values* command (A.3.5).
        For Aurora, it falls back to the ``VI`` command (B.7).
        """
        try:
            if self._protocol == "acoem":
                # Build payload: number of parameters (1 byte) + 2 bytes per parameter id.
                n = len(parameters)
                msg_data = bytes([n])
                for p in parameters:
                    msg_data += p.to_bytes(2, byteorder="big")
                msg = self._acoem_construct_message(command=5, payload=msg_data)
                response = self._tcpip_comm(msg, verbosity=verbosity)
                values = self._acoem_bytes2int(response, verbosity=verbosity)
                return dict(zip(parameters, values))

            elif self._protocol == "aurora":
                # Aurora VI command: VI<sid>xx where xx is parameter index.
                items: List[Any] = []
                for p in parameters:
                    if 0 <= p < 100:
                        wire = f"VI{self.serial_id:02d}{p:02d}\r".encode("ascii", errors="ignore")
                        resp = self._tcpip_comm(wire, verbosity=verbosity).decode("utf-8", errors="replace").strip()
                        items.append(resp)
                    else:
                        items.append("")
                return dict(zip(parameters, items))

            else:
                raise ValueError("Protocol not recognised in get_values().")
        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)
            return {}

    def set_value(self, parameter_id: int, value: int, verify: bool = True, verbosity: int = 0) -> int:
        """
        Set a parameter on the instrument.

        For ACOEM this uses command A.3.6 *Set Value*.
        For Aurora this is currently not implemented and raises ``NotImplementedError``.
        """
        if self._protocol != "acoem":
            raise NotImplementedError("set_value is currently only implemented for ACOEM protocol.")

        try:
            payload = value.to_bytes(4, byteorder="big", signed=False)
            msg = self._acoem_construct_message(command=6, parameter_id=parameter_id, payload=payload)
            _ = self._tcpip_comm(msg, verbosity=verbosity)
            if not verify:
                return value

            # Re-query the parameter
            resp = self.get_values([parameter_id], verbosity=verbosity)
            return int(resp.get(parameter_id, value))
        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)
            return value

    # ---- ID / version / datetime -------------------------------------

    def get_version(self, verbosity: int = 0) -> List[int]:
        """
        A.3.3 – Request the current firmware version (ACOEM only).

        Returns a list of two integers: [Build, Branch].
        """
        if self._protocol != "acoem":
            raise NotImplementedError("get_version is only available for ACOEM protocol.")
        try:
            msg = self._acoem_construct_message(2)
            resp = self._tcpip_comm(msg, verbosity=verbosity)
            return self._acoem_bytes2int(resp, verbosity=verbosity)
        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)
            return []

    def get_instr_type(self, verbosity: int = 0) -> List[int]:
        """A.3.2 – Request details on the analyser type (ACOEM only)."""
        if self._protocol != "acoem":
            raise NotImplementedError("get_instr_type is only available for ACOEM protocol.")
        try:
            msg = self._acoem_construct_message(1)
            resp = self._tcpip_comm(msg, verbosity=verbosity)
            return self._acoem_bytes2int(resp, verbosity=verbosity)
        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)
            return []

    def get_id(self, verbosity: int = 0) -> Dict[str, str]:
        """
        Get a human-readable instrument identification.

        For ACOEM this combines instrument type and firmware version.
        For Aurora this issues the ``ID`` command.
        """
        try:
            if self._protocol == "acoem":
                instr_type = self.get_instr_type(verbosity=verbosity)
                version = self.get_version(verbosity=verbosity)

                map_instr_type = {
                    "Model": {158: "ACOEM Aurora"},
                    "Variant": {300: "NE-300"},
                }
                model = map_instr_type["Model"].get(instr_type[0], str(instr_type[0]) if instr_type else "?")
                variant = map_instr_type["Variant"].get(
                    instr_type[1], str(instr_type[1]) if len(instr_type) > 1 else "?"
                )
                id_str = f"Model: {model} Variant: {variant}"
                if len(instr_type) > 2:
                    id_str += f" Sub-Type: {instr_type[2]}"
                if len(instr_type) > 3:
                    id_str += f" Range: {instr_type[3]}"
                if version:
                    id_str += f" Build: {version[0]} Branch: {version[1]}"
                resp = {"id": id_str}

            elif self._protocol == "aurora":
                wire = f"ID{self.serial_id}\r".encode("ascii", errors="ignore")
                txt = self._tcpip_comm(wire, verbosity=verbosity).decode("utf-8", errors="replace").strip()
                resp = {"id": txt}
            else:
                raise ValueError(f"[{self._name}] Communication protocol unknown")

            self.logger.info("[%s] get_id: %s", self._name, resp)
            return resp
        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)
            return {}

    def get_datetime(self, verbosity: int = 0) -> datetime:
        """Get the instrument's internal date and time."""
        try:
            if self._protocol == "acoem":
                msg = self._acoem_construct_message(4, 1)
                resp = self._tcpip_comm(msg, verbosity=verbosity)
                ints = self._acoem_bytes2int(resp, verbosity=verbosity)
                return self._acoem_timestamp_to_datetime(ints[0]) if ints else datetime.min

            elif self._protocol == "aurora":
                # Aurora returns a formatted ASCII string.
                txt = self._socket_comm(f"TI{self.serial_id:02d}")
                # Expected format e.g. "04/10/2024 12:34:56"
                return datetime.strptime(txt.strip(), "%d/%m/%Y %H:%M:%S")
            else:
                raise ValueError("Protocol not recognised in get_datetime().")
        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)
            return datetime.min

    def set_datetime(self) -> None:  # type: ignore[override]
        """
        Set the instrument's internal clock to the current system time.

        For ACOEM this uses the binary *Set Date/Time* command.
        For Aurora this is currently not implemented.
        """
        verbosity = 0
        try:
            now = datetime.now()
            if self._protocol == "acoem":
                payload = self._acoem_datetime_to_timestamp(now)
                msg = self._acoem_construct_message(3, 1, payload=payload)
                _ = self._tcpip_comm(msg, verbosity=verbosity)
            else:
                self.logger.info("[%s] set_datetime: not implemented for protocol=%s", self._name, self._protocol)
        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)

    # ---- current operation --------------------------------------------

    def get_current_operation(self, verbosity: int = 0) -> int:
        """
        Return current operation state.

        ACOEM: parameter 4035 (0=ambient, 1=zero, 2=span).
        Aurora: not yet implemented.
        """
        if self._protocol != "acoem":
            raise NotImplementedError("get_current_operation is only implemented for ACOEM.")
        try:
            resp = self.get_values([4035], verbosity=verbosity)
            return int(resp.get(4035, -1))
        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)
            return -1

    def set_current_operation(self, state: int = 0, verify: bool = True, verbosity: int = 0) -> int:
        """
        Set the instrument operating state by actuating the internal valve.

        state: 0 ambient, 1 zero, 2 span
        """
        if self._protocol != "acoem":
            raise NotImplementedError("set_current_operation is only implemented for ACOEM.")
        try:
            return self.set_value(4035, state, verify=verify, verbosity=verbosity)
        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)
            return state

    # ---- logged data --------------------------------------------------

    def get_logged_data(self, verbosity: int = 0) -> Any:
        """
        Placeholder for logged data download.

        The legacy driver implemented several variants of the NE-300 / Aurora
        logged-data commands (***R, ACOEM "Get Logged Data" etc.).  These can
        be ported here when needed.  For now, this method raises
        ``NotImplementedError`` to make the intent explicit.
        """
        raise NotImplementedError("get_logged_data has not yet been ported from the legacy driver.")

    # ------------------------------------------------------------------
    # Instrument.abstract API
    # ------------------------------------------------------------------

    def accumulate_data(self, data: str) -> None:  # type: ignore[override]
        """Append a line of CSV text to the internal buffer."""
        if not data:
            return
        if not data.endswith("\n"):
            data += "\n"
        with self._buf_lock:
            self._data += data

    def get_current_data(self, sep: str = ",") -> str:
        """
        Retrieve a near real-time snapshot from the instrument.

        - ACOEM: queries a fixed list of parameters via :meth:`get_values`
          and returns a comma-separated string ``dtm,<values...,state>``.
        - Aurora: sends ``VI099`` over the configured transport.
        """
        try:
            if self._protocol == "acoem":
                params = list(self._ACOEM_CURRENT_PARAMS)
                data = self.get_values(params)
                ts = data.get(1)
                if isinstance(ts, int):
                    ts = self._acoem_timestamp_to_datetime(ts)
                elif isinstance(ts, str):
                    # allow already formatted timestamp
                    ts = datetime.fromisoformat(ts)
                if not isinstance(ts, datetime):
                    ts = datetime.now()

                values: List[str] = []
                for p in params[1:]:
                    v = data.get(p, np.nan)
                    values.append(f"{float(v):.6g}")
                line = sep.join((_iso(ts), *values))
                return line

            elif self._protocol == "aurora":
                if self._params_comms == "serial":
                    raw = self._serial_comm("VI099")
                else:
                    raw = self._socket_comm(f"VI{self.serial_id:02d}99")
                # Normalise commas & line endings
                raw = raw.replace(", ", ",")
                return raw.strip()
            else:
                raise ValueError("Protocol not recognised in get_current_data().")
        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)
            return ""

    def parse_current_data(self, reading: str) -> Tuple[datetime, np.ndarray]:
        """
        Parse a single reading into (timestamp, values).

        Aurora: expects ``YYYY-mm-dd HH:MM:SS,<floats...>,<status_hex>``.
        ACOEM: parses the CSV line produced by :meth:`get_current_data`.
        """
        parts = [p.strip() for p in reading.split(",") if p.strip()]
        if not parts:
            raise ValueError("Empty reading in parse_current_data().")

        # Timestamp is always first field
        ts = datetime.strptime(parts[0], "%Y-%m-%d %H:%M:%S")
        if self._protocol == "aurora" and len(parts) >= 2:
            floats = [float(x) for x in parts[1:-1]]
            status = int(parts[-1], 16)
            arr = np.array([*floats, float(status)], dtype=float)
            return ts, arr
        else:
            # Generic numeric parse (ACOEM CSV)
            vals: List[float] = []
            for tok in parts[1:]:
                try:
                    vals.append(float(tok))
                except ValueError:
                    vals.append(np.nan)
            return ts, np.asarray(vals, dtype=float)

    def get_data(self) -> str:  # type: ignore[override]
        """
        Acquire one current data line and append it to the internal buffer.

        Returns the CSV line as a string.
        """
        line = self.get_current_data(sep=",")
        if line:
            self.accumulate_data(line)
        return line

    # ---- config & display stubs --------------------------------------

    def get_config(self) -> dict:  # type: ignore[override]
        """
        Return a minimal configuration snapshot queried from the device.

        Currently this is limited to ID and, for ACOEM, firmware version.
        """
        cfg: Dict[str, Any] = {}
        try:
            ident = self.get_id()
            if ident:
                cfg.update(ident)
        except Exception:
            pass

        if self._protocol == "acoem":
            try:
                version = self.get_version()
                if version:
                    cfg["firmware_build"] = version[0]
                    cfg["firmware_branch"] = version[1] if len(version) > 1 else None
            except Exception:
                pass
        return cfg

    def set_config(self) -> dict:  # type: ignore[override]
        """
        Apply configuration to the device.

        At the moment this is a placeholder and returns an empty dict.  Use the
        high-level methods (:meth:`set_value`, :meth:`set_datetime`, etc.) to
        change individual settings.
        """
        return {}

    def display_data(self) -> None:  # type: ignore[override]
        """Log a brief human-readable summary of the current readings."""
        try:
            line = self.get_data()
            if line:
                self.logger.info("[%s] %s", self._name, line)
        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)


class NE300(NEPH):
    """Backwards-compatible NE300 driver that forces ACOEM protocol."""

    def __init__(self, config_path: str, name: str = "NE300") -> None:
        super().__init__(config_path=config_path, name=name)
        self._protocol = "acoem"


class Aurora3000(NEPH):
    """Backwards-compatible Aurora3000 driver that forces Aurora protocol."""

    def __init__(self, config_path: str, name: str = "Aurora3000") -> None:
        super().__init__(config_path=config_path, name=name)
        self._protocol = "aurora"


__all__ = ["NEPH", "NE300", "Aurora3000"]
