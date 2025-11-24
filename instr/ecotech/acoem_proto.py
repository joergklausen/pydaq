from __future__ import annotations

import socket
import struct
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Tuple

import colorama
import numpy as np

from ..instrument import Instrument


class AcoemClient:
    """
    Implementation of the binary ACOEM protocol for NE-300.

    This class does *not* know about YAML, staging, parquet, etc. It only
    knows how to talk ACOEM and interpret the responses.

    The NEPH driver passes itself in as `driver`; we only use a few of its
    attributes:

    - `_sockaddr`, `_socktout`, `_socksleep` for TCP parameters
    - `_io_lock` to serialize I/O across threads
    - `logger` for logging
    - `sampling_interval` as a default for the data-logger interval
    """

    # ACOEM command bytes (cf. Aurora NE Series manual, Appendix A)
    CMD_GET_VALUES = 4
    CMD_SET_VALUE = 5
    CMD_GET_DATALOG_CONFIG = 6
    CMD_GET_LOGGED_DATA = 7

    def __init__(self, driver: Instrument, params: Dict[str, Any]) -> None:
        self._driver = driver
        self._name = getattr(driver, "_name", "AcoemClient")
        self.logger = driver.logger

        # Serial ID (SID)
        try:
            self.serial_id = int(getattr(driver, "_serial_id", params.get("serial_id", 1)))
        except Exception:
            self.serial_id = 1

        # Optional hints from config for data-logger
        data_log_cfg = params.get("data_log", {}) or {}
        self._data_log_params: List[int] = [
            int(p) for p in data_log_cfg.get("parameters", []) if p is not None
        ]
        try:
            # Use YAML data_log.interval if present, otherwise fall back to the
            # driver's sampling_interval (in seconds) and finally 60 s.
            self._datalog_interval = int(
                data_log_cfg.get("interval", getattr(driver, "sampling_interval", 60))
            )
        except Exception:
            self._datalog_interval = getattr(driver, "sampling_interval", 60)

    # ------------------------------------------------------------------
    # Low-level ACOEM helpers
    # ------------------------------------------------------------------

    def _checksum(self, x: bytes) -> bytes:
        """
        Compute the XOR checksum over all bytes in ``x``.

        Reference: Aurora NE Series User Manual, Appendix A.1.
        """
        check_sum = 0
        for _byte in x:
            check_sum ^= _byte
        return bytes([check_sum])

    def _build_message(self, command: int, parameter_id: int = 0, payload: bytes = b"") -> bytes:
        """
        Construct ACOEM packet to be sent to instrument. See the ACOEM manual for explanations.
        
        Byte  |1  |2  |3  |4  |5..6     |7..10    |11       |12
              |STX|SID|CMD|ETX|msg_len  |msg_data |checksum |EOT
        STX = chr(2)
        SID = serial_id
        CMD = command
        ETX = chr(3)
        msg_len = message length
        msg_data = message data
        EOT = chr(4)

        Args:
            command (int): cf. ACOEM manual Table 19 - List of Commands
            parameter_id (int, optional): cf. ACOEM manual Table 46 - Aurora Parameters. Defaults to 0 (Not a valid parameter).
            payload (int, optional): _description_. Defaults to None.

        Returns:
            bytes: _description_
        """
        msg_data = b""
        if parameter_id > 0:
            msg_data += parameter_id.to_bytes(4, byteorder="big")
        if payload:
            msg_data += payload

        msg_len = len(msg_data)
        header = bytes([2, self.serial_id, command, 3]) + msg_len.to_bytes(2, byteorder="big")
        msg = header + msg_data
        return msg + self._checksum(msg) + bytes([4])

    def _timestamp_to_datetime(self, timestamp: int) -> datetime:
        """
        Convert an ACOEM packed timestamp to :class:`datetime`.

        Bit layout (LSB first):

        - 6 bits: seconds
        - 6 bits: minutes
        - 5 bits: hours
        - 5 bits: day
        - 4 bits: month
        - 6 bits: year offset from 2000
        """
        dtm = int(timestamp)
        ss = dtm % 64
        dtm //= 64
        mm = dtm % 64
        dtm //= 64
        hh = dtm % 32
        dtm //= 32
        day = dtm % 32
        dtm //= 32
        month = dtm % 16
        year = (dtm // 16) + 2000

        try:
            return datetime(year, month, day, hh, mm, ss, tzinfo=timezone.utc)
        except Exception:
            # Fallback to "epoch" if something is off
            return datetime(1970, 1, 1, tzinfo=timezone.utc)

    def _datetime_to_timestamp(self, dtm: datetime) -> bytes:
        """
        Convert :class:`datetime` to ACOEM packed timestamp (4-byte big-endian).
        """
        if dtm.tzinfo is None:
            dtm = dtm.replace(tzinfo=timezone.utc)
        dtm = dtm.astimezone(timezone.utc)

        ss = format(dtm.second, "06b")
        mm = format(dtm.minute, "06b")
        hh = format(dtm.hour, "05b")
        day = format(dtm.day, "05b")
        month = format(dtm.month, "04b")
        year = format(dtm.year - 2000, "06b")

        bits = year + month + day + hh + mm + ss
        value = int(bits, 2)
        return value.to_bytes(4, byteorder="big")

    def _tcp_request(self, message: bytes, expect_response: bool = True, verbosity: int = 0) -> bytes:
        """
        Low-level TCP exchange for binary ACOEM messages.

        Uses the socket configuration from the attached Instrument:
        - ``_sockaddr``  -> (host, port)
        - ``_socktout``  -> timeout in seconds
        - ``_socksleep`` -> sleep between recv calls
        - ``_io_lock``   -> to serialize access across threads

        Returns the raw bytes received from the instrument, including the full
        ACOEM frame (STX...EOT). Telnet prelude stripping and frame decoding
        are handled by the higher-level decode helpers.
        """
        drv = self._driver
        host, port = getattr(drv, "_sockaddr", ("127.0.0.1", 0))
        timeout = float(getattr(drv, "_socktout", 2.0))
        sleep = float(getattr(drv, "_socksleep", 0.1))
        lock = getattr(drv, "_io_lock", None)

        if not port:
            raise RuntimeError(f"[{self._name}] TCP socket not configured on driver (port=0).")

        def _exchange() -> bytes:
            data = b""
            start = time.perf_counter()
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                sock.connect((host, port))

                if verbosity > 1:
                    self.logger.debug(
                        f"{colorama.Fore.CYAN}[{self._name}] → {message!r}{colorama.Fore.GREEN}"
                    )

                sock.sendall(message)

                if not expect_response:
                    return b""

                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                    # ACOEM frames end with EOT (0x04)
                    if b"\x04" in chunk:
                        break
                    if sleep > 0:
                        time.sleep(sleep)

            if verbosity > 1:
                elapsed = time.perf_counter() - start
                self.logger.debug(
                    f"{colorama.Fore.CYAN}[{self._name}] ← {len(data)} bytes in {elapsed:.3f}s{colorama.Fore.GREEN}"
                )

            return data

        if lock is None:
            return _exchange()
        # serialize TCP I/O with the same lock used for serial operations
        with lock:
            return _exchange()

    def _bytes_to_ints(self, response: bytes, verbosity: int = 0) -> List[int]:
        """
        Convert an ACOEM response payload into a list of 32-bit unsigned integers.

        Used for responses like "Get data-logger config".
        """
        if len(response) < 8:
            return []
        msg_len_bytes = int.from_bytes(response[4:6], byteorder="big", signed=False)
        msg_data = response[6 : 6 + msg_len_bytes]

        items: List[int] = []
        for i in range(0, len(msg_data), 4):
            chunk = msg_data[i : i + 4]
            if len(chunk) != 4:
                continue
            items.append(int.from_bytes(chunk, byteorder="big", signed=False))

        if verbosity > 1:
            self.logger.debug(f"[{self._name}] ACOEM bytes → ints: {items}")
        return items

    def _is_integer_param(self, parameter: int) -> bool:
        """
        Heuristic to decide whether a parameter ID represents an integer.

        Based on the ranges used in the original driver: some parameters are
        counters/indices, others are floats.
        """
        if 1000 < parameter < 5000:
            return True
        if 12_000_000 < parameter < 13_000_000:
            return True
        if 14_000_000 < parameter < 15_000_000:
            return True
        return False

    def _decode_error(self, error_code: int) -> str:
        """
        Map ACOEM error code to a human-readable message.

        This is not an exhaustive mapping; unknown codes fall back to a generic message.
        """
        error_map = {
            0: "checksum failed",
            1: "invalid command byte",
            2: "invalid parameter",
            3: "invalid message length",
            4: "reserved",
            5: "reserved",
            6: "reserved",
        }
        return error_map.get(error_code, f"unknown error code {error_code}")

    def _decode_values_response(
        self,
        parameters: List[int],
        response: bytes,
        verbosity: int = 0,
    ) -> Dict[int, Any]:
        """
        Decode the response to a ``Get Values`` command into a dict.

        Returns ``{parameter_id: value}``, where:

        - Parameter 1 / 2201 are decoded as :class:`datetime`
        - Integer-like parameters are decoded as signed ints
        - Remaining values are decoded as big-endian floats
        """
        if len(response) < 8:
            return {}

        # Error frame?
        if response[2] == 0 and len(response) > 7:
            err_code = response[7]
            msg = self._decode_error(err_code)
            raise RuntimeError(f"ACOEM error {err_code}: {msg}")

        msg_len_bytes = int.from_bytes(response[4:6], byteorder="big", signed=False)
        msg_data = response[6 : 6 + msg_len_bytes]
        words = [msg_data[i : i + 4] for i in range(0, len(msg_data), 4) if len(msg_data[i : i + 4]) == 4]

        if len(words) != len(parameters):
            raise ValueError(
                f"Number of parameters ({len(parameters)}) does not match items in response ({len(words)})."
            )

        data: Dict[int, Any] = {}
        for parameter, raw in zip(parameters, words):
            if parameter in (1, 2201):
                ts = int.from_bytes(raw, byteorder="big", signed=False)
                data[parameter] = self._timestamp_to_datetime(ts)
            elif self._is_integer_param(parameter):
                data[parameter] = struct.unpack(">i", raw)[0]
            else:
                data[parameter] = struct.unpack(">f", raw)[0]

        if verbosity > 0:
            self.logger.debug(f"[{self._name}] ACOEM GetValues decoded: %s", data)
        return data

    def _decode_logged_data(
        self,
        response: bytes,
        digits: int = 5,
        verbosity: int = 0,
    ) -> List[Dict[str | int, Any]]:
        """
        Decode a ``Get Logged Data`` response (command 7) into a list of records.

        Each record is returned as a dict:

        .. code-block:: python

            {
                <param_id_1>: value_1,
                <param_id_2>: value_2,
                ...,
                "logging_interval": <int seconds>,
                "dtm": "YYYY-MM-DD HH:MM:SS",
            }
        """
        if len(response) < 8:
            return []

        cmd = response[2]
        if cmd != self.CMD_GET_LOGGED_DATA:
            return []

        # Skip STX, SID, CMD, MSGTYPE, LEN
        # MSGTYPE == 3 for success; 0 for error
        msg_type = response[3]
        if msg_type == 0 and len(response) > 7:
            # Error frame
            err_code = response[7]
            msg = self._decode_error(err_code)
            self.logger.error(
                f"{colorama.Fore.RED}[{self._name}] Get Logged Data error: {msg}{colorama.Fore.GREEN}"
            )
            return []

        # Payload starts at byte 4, after STX, SID, CMD
        # See ACOEM documentation for exact layout.
        all_records: List[Dict[str | int, Any]] = []

        # The frame may contain multiple records; we walk the buffer.
        offset = 0
        while offset + 16 <= len(response):
            # Each record: time(4) + interval(4) + n_fields(4) + values(n*4)
            rec = response[offset:]
            if len(rec) < 16:
                break

            ts_bytes = rec[0:4]
            interval_bytes = rec[4:8]
            n_fields_bytes = rec[8:12]

            ts_int = int.from_bytes(ts_bytes, byteorder="big", signed=False)
            logging_interval = int.from_bytes(interval_bytes, byteorder="big", signed=False)
            n_fields = int.from_bytes(n_fields_bytes, byteorder="big", signed=False)

            needed = 12 + n_fields * 4
            if len(rec) < needed:
                break

            # Parameter IDs
            keys = [
                int.from_bytes(rec[12 + i * 4 : 12 + (i + 1) * 4], byteorder="big", signed=False)
                for i in range(n_fields)
            ]

            # Values as raw 4-byte sequences
            value_bytes = [
                rec[12 + n_fields * 4 + j * 4 : 12 + n_fields * 4 + (j + 1) * 4]
                for j in range(n_fields)
            ]
            mapping = dict(zip(keys, value_bytes))

            decoded: Dict[str | int, Any] = {}
            for pid, raw in mapping.items():
                # Logged values > 1000 are floats in practice
                if pid > 1000 and len(raw) == 4:
                    decoded[pid] = round(struct.unpack(">f", raw)[0], digits)
                else:
                    decoded[pid] = int.from_bytes(raw, byteorder="big", signed=False)

            dtm = self._timestamp_to_datetime(ts_int)
            decoded["logging_interval"] = logging_interval
            decoded["dtm"] = dtm.strftime("%Y-%m-%d %H:%M:%S")

            if verbosity > 0:
                self.logger.debug(f"[{self._name}] ACOEM logged-data record: {decoded}")

            all_records.append(decoded)
            offset += needed

        return all_records

    # ------------------------------------------------------------------
    # Public protocol API used by NEPH
    # ------------------------------------------------------------------

    def get_values(self, parameters: Iterable[int], verbosity: int = 0) -> Dict[int, Any]:
        """
        Execute the ACOEM *Get Values* (CMD 4) call for the given parameters.

        Returns:
            dict[int, Any]: Mapping ``parameter_id -> decoded value``.
        """
        try:
            param_list = list(parameters)
            payload = b"".join(int(p).to_bytes(4, "big") for p in param_list)
            msg = self._build_message(self.CMD_GET_VALUES, payload=payload)
            raw = self._tcp_request(msg, expect_response=True, verbosity=verbosity)
            return self._decode_values_response(param_list, raw, verbosity=verbosity)
        except Exception as err:
            self.logger.error(
                f"{colorama.Fore.RED}[{self._name}] get_values failed: {err}{colorama.Fore.GREEN}"
            )
            return {}

    def set_value(self, parameter_id: int, value: int | float, verbosity: int = 0) -> bool:
        """
        Execute the ACOEM *Set Value* (CMD 5) for a single parameter.
        """
        try:
            if isinstance(value, float):
                payload = struct.pack(">f", value)
            else:
                payload = struct.pack(">I", int(value))

            msg = self._build_message(self.CMD_SET_VALUE, parameter_id=parameter_id, payload=payload)
            raw = self._tcp_request(msg, expect_response=True, verbosity=verbosity)
            ints = self._bytes_to_ints(raw)
            if len(ints) < 3 or ints[2] != 0:
                raise RuntimeError(f"set_value failed for parameter {parameter_id}, response ints={ints}")
            return True
        except Exception as err:
            self.logger.error(
                f"{colorama.Fore.RED}[{self._name}] set_value failed: {err}{colorama.Fore.GREEN}"
            )
            return False

    def get_datetime(self, verbosity: int = 0) -> datetime:
        """
        Get the instrument datetime via ACOEM.

        The NE driver maps parameter 1 to instrument datetime (see original code).
        """
        try:
            dtm = self.get_values([1], verbosity=verbosity)[1]
            if isinstance(dtm, datetime):
                return dtm
        except Exception as err:
            self.logger.error(f"{colorama.Fore.RED}[{self._name}] get_datetime failed: {err}{colorama.Fore.GREEN}")
        return datetime(1970, 1, 1, tzinfo=timezone.utc)

    def set_datetime(self, dtm: datetime, verbosity: int = 0) -> None:
        """
        Set the instrument datetime by writing parameter 1.
        """
        try:
            ts_bytes = self._datetime_to_timestamp(dtm)
            ts_int = int.from_bytes(ts_bytes, byteorder="big", signed=False)
            self.set_value(1, ts_int, verbosity=verbosity)
        except Exception as err:
            self.logger.error(f"{colorama.Fore.RED}[{self._name}] set_datetime failed: {err}{colorama.Fore.GREEN}")

    def get_id(self, verbosity: int = 0) -> Dict[str, str]:
        """
        Return a minimal identity mapping for the instrument.

        You can extend this to use specific parameters for firmware / model, etc.
        """
        ident: Dict[str, str] = {"protocol": "acoem", "serial_id": str(self.serial_id)}
        try:
            dtm = self.get_datetime(verbosity=verbosity)
            ident["datetime"] = dtm.isoformat()
        except Exception:
            pass
        return ident

    def get_data_log_config(self, verbosity: int = 0) -> List[int]:
        """
        Retrieve the current data-logger configuration from the instrument.

        Returns:
            list[int]: ``[interval_seconds, param1, param2, ...]``

        The internal ``_data_log_params`` and ``_datalog_interval`` are updated
        from the instrument’s response (regardless of what the YAML contained).
        """
        try:
            msg = self._build_message(self.CMD_GET_DATALOG_CONFIG)
            raw = self._tcp_request(msg, expect_response=True, verbosity=verbosity)
            ints = self._bytes_to_ints(raw)
            if len(ints) < 5:
                raise RuntimeError(f"get_data_log_config: unexpected response ints={ints}")

            interval = ints[3]
            params = ints[4:]

            self._datalog_interval = int(interval)
            if not self._data_log_params:
                self._data_log_params = [int(p) for p in params]

            return [self._datalog_interval, *self._data_log_params]
        except Exception as err:
            self.logger.error(
                f"{colorama.Fore.RED}[{self._name}] get_data_log_config failed: {err}{colorama.Fore.GREEN}"
            )
            return []

    def set_datalog_interval(self, verbosity: int = 0) -> int:
        """
        Set the data-logger interval via ACOEM parameter 2002.

        Returns the interval in seconds as reported by the instrument.
        """
        try:
            interval = self.set_value(
                2002,
                self._datalog_interval,
                verbosity=verbosity,
            )
            return int(interval)
        except Exception as err:
            self.logger.error(
                f"{colorama.Fore.RED}[{self._name}] set_datalog_interval failed: {err}{colorama.Fore.GREEN}"
            )
            return 0

    def get_logged_data(
        self,
        start: datetime,
        end: datetime | None = None,
        verbosity: int = 0,
    ) -> List[Dict[str | int, Any]]:
        """
        Retrieve aggregated data from the instrument data-logger between
        ``start`` and ``end`` (inclusive).
        """
        if end is None:
            end = start

        if start > end:
            raise ValueError(f"[{self._name}] start must not be after end")

        try:
            payload = self._datetime_to_timestamp(start) + self._datetime_to_timestamp(end)
            msg = self._build_message(self.CMD_GET_LOGGED_DATA, payload=payload)
            raw = self._tcp_request(msg, expect_response=True, verbosity=verbosity)
            data = self._decode_logged_data(raw, digits=5, verbosity=verbosity)
            return data
        except Exception as err:
            self.logger.error(
                f"{colorama.Fore.RED}[{self._name}] get_logged_data failed: {err}{colorama.Fore.GREEN}"
            )
            return []
        
    def logged_data_to_csv(self, records: List[Dict[str | int, Any]], sep: str = ",") -> str:
        """
        Convert logged-data records (from :meth:`get_logged_data`) into CSV text.
        """
        if not records:
            return ""

        lines: List[str] = []

        # Header: dtm + all other keys in the insertion order of the first record
        first = dict(records[0])
        dtm_key = "dtm"
        if dtm_key in first:
            first.pop(dtm_key)
        keys = list(first.keys())

        header = sep.join([dtm_key] + [str(k) for k in keys])
        lines.append(header)

        for rec in records:
            d = dict(rec)
            dtm_value = d.pop(dtm_key, "")
            values = [str(dtm_value)] + [str(d.get(k, "")) for k in keys]
            lines.append(sep.join(values))

        return "\n".join(lines) + "\n"

    def get_current_operation(self, verbosity: int = 0) -> int:
        """
        Retrieve operating state via Aurora parameter 4035 (NE-300) using ACOEM.

        0 = Normal monitoring
        1 = Zero calibration/check
        2 = Span calibration/check
        9 = Error / unknown
        """
        try:
            val = self.get_values([4035], verbosity=verbosity).get(4035, 9)
            return int(val)
        except Exception as err:
            self.logger.error(f"{colorama.Fore.RED}[{self._name}] get_current_operation failed: {err}{colorama.Fore.GREEN}")
            return 9

    def set_current_operation(
        self,
        state: int = 0,
        verify: bool = True,
        verbosity: int = 0,
    ) -> int:
        """
        Set instrument operating state via Aurora parameter 4035.

        See :meth:`get_current_operation` for meaning of ``state``.
        """
        try:
            return self.set_value(4035, state, verbosity=verbosity)
        except Exception as err:
            self.logger.error(f"{colorama.Fore.RED}[{self._name}] set_current_operation failed: {err}{colorama.Fore.GREEN}")
            return 9

    # ------------------------------------------------------------------
    # Live "current data" helpers used by NEPH.get_data()
    # ------------------------------------------------------------------

    def get_current_data(self, sep: str = ",") -> str:
        """
        Build a simple CSV line from a *Get Values* (CMD 4) call.

        Semantics
        ---------
        - This returns an *instantaneous* snapshot: one set of current values
          at the moment the command is executed.
        - It does **not** depend on the data-logger interval or on
          ``sampling_interval`` – the instrument simply returns the current
          values of the requested parameters.
        - The logged data retrieved via :meth:`get_logged_data` aggregate many
          such instantaneous readings over complete data-logger intervals.

        Columns
        -------
        - The first column is always the instrument timestamp (parameter 1),
          formatted as ``YYYY-MM-DD HH:MM:SS``.
        - The remaining columns correspond to the parameters configured in the
          data-logger (if available), in the same order as the logger.
        """
        try:
            # Pick parameter set: [1] + data-logger parameters.
            params = [1]
            if self._data_log_params:
                params.extend(self._data_log_params)
            else:
                cfg = self.get_data_log_config()
                fields = cfg[1:] if cfg and len(cfg) > 1 else []
                if fields:
                    self._data_log_params = [int(p) for p in fields]
                    params.extend(self._data_log_params)

            vals = self.get_values(params)

            # Ensure we have a datetime for parameter 1
            dtm = vals.get(1)
            if isinstance(dtm, datetime):
                dtm_str = dtm.strftime("%Y-%m-%d %H:%M:%S")
            else:
                dtm_str = ""

            body = [str(vals.get(p, "")) for p in params if p != 1]
            return sep.join([dtm_str, *body]) + "\n"
        except Exception as err:
            self.logger.error(
                f"{colorama.Fore.RED}[{self._name}] get_current_data failed: {err}{colorama.Fore.GREEN}"
            )
            return ""

    def parse_current_data(self, reading: str) -> Tuple[datetime, np.ndarray]:
        """
        Parse a CSV line produced by :meth:`get_current_data`.

        The first column is parsed as ``YYYY-mm-dd HH:MM:SS``, remaining values
        are converted to floats.
        """
        parts = [p.strip() for p in reading.split(",") if p.strip()]
        if not parts:
            raise ValueError("Empty reading")
        ts = datetime.strptime(parts[0], "%Y-%m-%d %H:%M:%S")
        vals = np.array([float(x) for x in parts[1:]], dtype=float) if len(parts) > 1 else np.array([])
        return ts, vals

    # ------------------------------------------------------------------
    # Config snapshots for NEPH.get_config / set_config
    # ------------------------------------------------------------------

    def get_config_snapshot(self) -> Dict[str, Any]:
        cfg: Dict[str, Any] = {}
        try:
            cfg["datalog_config"] = self.get_data_log_config()
        except Exception:
            pass
        try:
            cfg["datalog_interval"] = self.set_datalog_interval(verbosity=0)
        except Exception:
            pass
        return cfg

    def set_config_snapshot(self) -> Dict[str, Any]:
        """
        Apply configuration items based on YAML hints (e.g. datalog interval).

        At the moment this only sets the data-logger interval.
        """
        out: Dict[str, Any] = {}
        try:
            out["datalog_interval"] = self.set_datalog_interval(verbosity=0)
        except Exception:
            pass
        return out
