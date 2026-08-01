from __future__ import annotations

"""Ecotech / ACOEM nephelometer driver for pydaq.

The driver supports two related instrument protocols:

* ``io.protocol: aurora`` for the Ecotech Aurora 3000 ASCII protocol.
* ``io.protocol: acoem`` for the ACOEM binary protocol used by the NE-300.

``driver: aurora3000`` defaults to the Aurora protocol and ``driver: ne300``
defaults to the ACOEM protocol.  The explicit ``io.protocol`` setting remains
available for diagnostics and unusual installations.

The shared :class:`NEPH` driver reads instantaneous values and can aggregate
them with :class:`TimeBucketAggregator`.  The :class:`NE300` subclass retrieves
the complete records from the instrument's internal data logger using ACOEM
command 7 and writes every returned record.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import inspect
import socket
import struct
import time
from typing import Any, Iterable, Mapping

from pydaq.instruments.instrument import Instrument, LineComms, TimeBucketAggregator


class NEPH(Instrument):
    """Driver for Aurora 3000 and NE-300 nephelometers.

    The output columns preserve the historical Aurora 3000 current-value
    format.  For the ACOEM protocol, equivalent NE-300 parameter IDs are mapped
    onto the same column names.
    """

    HEADERS = [
        "dtm",
        "ssp1",
        "ssp2",
        "ssp3",
        "sbsp1",
        "sbsp2",
        "sbsp3",
        "sample_temp",
        "enclosure_temp",
        "RH",
        "pressure",
        "major_state",
        "DIO_state",
    ]
    AURORA_VI99_VALUE_FIELDS = HEADERS[1:]

    # Equivalent to the Aurora VI099 current-data response.  Parameter 4035
    # is CURRENT_OPERATION and therefore maps to ``major_state``; parameter
    # 4036 maps to ``DIO_state``.  This is also consistent with the NE-300
    # logged-data record header, where CURRENT_OPERATION is carried as 4035.
    ACOEM_CURRENT_PARAMETER_IDS = [
        1,
        1635000,
        1525000,
        1450000,
        1635090,
        1525090,
        1450090,
        5001,
        5004,
        5003,
        5002,
        4035,
        4036,
    ]
    ACOEM_PARAMETER_TO_FIELD = dict(zip(ACOEM_CURRENT_PARAMETER_IDS, HEADERS))

    _TELNET_NEGOTIATION = b"\xff\xfb\x01\xff\xfe\x01\xff\xfb\x03"

    def initialize(self) -> None:
        """Read driver parameters and initialize communication helpers."""
        params = self._params()
        io_cfg = self._resolve_io_config(params)
        schedule_cfg = self._optional_mapping(params, "schedule")
        processing_cfg = self._optional_mapping(params, "processing")
        init_cfg = self._optional_mapping(params, "init")

        aggregate_value = processing_cfg.get("aggregate")
        aggregate_cfg = (
            dict(aggregate_value)
            if isinstance(aggregate_value, Mapping)
            else self._optional_mapping(params, "aggregate")
        )

        self.protocol = self._resolve_protocol(params, io_cfg)
        self.serial_id = self._as_int(
            params.get("serial_id", params.get("id", 0)),
            name="serial_id",
        )
        self.io_kind = self._io_kind(
            io_cfg,
            default="serial" if self.protocol == "aurora" else "tcp",
        )
        self.comms: LineComms | None = None

        if self.protocol == "aurora":
            self.comms = LineComms(dict(io_cfg), logger=self.logger)
        else:
            self._init_acoem_tcp(io_cfg)

        self.acoem_parameters = self._resolve_acoem_parameters(
            params,
            processing_cfg,
        )
        self.aggregator = self._build_aggregator(
            schedule_cfg,
            processing_cfg,
            aggregate_cfg,
        )
        self.empty_record_is_ok = self.aggregator is not None

        self.logger.info(
            "[%s] initialized Ecotech/ACOEM driver "
            "protocol=%s io=%s serial_id=%s aggregation=%s",
            self.name,
            self.protocol,
            self.io_kind,
            self.serial_id,
            "on" if self.aggregator else "off",
        )

        if self._as_bool(
            init_cfg.get(
                "id_on_initialize",
                params.get("id_on_initialize", False),
            )
        ):
            ident = self.get_instrument_id()
            if ident:
                self.logger.info("[%s] instrument id: %r", self.name, ident)
            else:
                self.logger.warning(
                    "[%s] instrument id query returned no response",
                    self.name,
                )

    def get_record(self) -> dict[str, Any]:
        """Return one formatted record or ``{}`` while an aggregate is open."""
        try:
            sample = self.get_current_sample()
            if not sample:
                return {}

            if self.aggregator is None:
                return self._format_record(sample)

            aggregate = self.aggregator.add(sample)
            if aggregate is None:
                return {}
            return self._format_record(aggregate)

        except Exception as exc:
            self.logger.error(
                "[%s] Ecotech get_record failed: %s",
                self.name,
                exc,
                exc_info=True,
            )
            return {}

    def collect_record(self) -> dict[str, Any]:
        """Compatibility alias for older pydaq driver code."""
        return self.get_record()

    # ------------------------------------------------------------------
    # Public diagnostic helpers
    # ------------------------------------------------------------------

    def get_current_sample(self) -> dict[str, Any]:
        """Return one instantaneous sample before optional aggregation."""
        if self.protocol == "aurora":
            return self._get_aurora_current_sample()
        if self.protocol == "acoem":
            return self._get_acoem_current_sample()
        raise ValueError(
            f"[{self.name}] unsupported protocol {self.protocol!r}"
        )

    def get_instrument_id(self) -> str:
        """Return an instrument identification string, when supported."""
        if self.protocol == "aurora":
            return self._aurora_request(f"ID{self.serial_id}").strip()

        if self.protocol == "acoem":
            instr_type = self.get_instr_type()
            version = self.get_version()
            if not instr_type and not version:
                return ""
            return f"type={instr_type} version={version}"

        return ""

    def get_status_word(self) -> str:
        """Return the Aurora status word via VI088."""
        if self.protocol != "aurora":
            return ""
        return self._aurora_request(f"VI{self.serial_id}88").strip()

    def read_new_data(self) -> str:
        """Return available Aurora logger data from the instrument cursor."""
        if self.protocol != "aurora":
            return ""
        return self._aurora_request("***D")

    # ------------------------------------------------------------------
    # Configuration parsing
    # ------------------------------------------------------------------

    def _params(self) -> dict[str, Any]:
        value = getattr(self, "parameters", None)
        if not isinstance(value, dict):
            raise ValueError(
                f"[{self.name}] missing driver parameters; expected a dict."
            )
        return value

    def _resolve_io_config(
        self,
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        io_value = params.get("io")
        if isinstance(io_value, Mapping):
            return dict(io_value)

        # Backward compatibility for early draft configs and legacy names.
        legacy: dict[str, Any] = {}
        if "serial_port" in params or "port" in params:
            legacy["kind"] = "serial"
            legacy["port"] = params.get(
                "serial_port",
                params.get("port", ""),
            )
            legacy["baudrate"] = params.get(
                "serial_baudrate",
                params.get("baudrate", 19200),
            )
            legacy["timeout_seconds"] = params.get(
                "serial_timeout",
                params.get("timeout", 2.0),
            )
        elif "socket" in params and isinstance(
            params.get("socket"),
            Mapping,
        ):
            socket_cfg = dict(params["socket"])
            legacy["kind"] = "tcp"
            legacy["host"] = socket_cfg.get("host", "")
            legacy["port"] = socket_cfg.get("port", 0)
            legacy["timeout_seconds"] = socket_cfg.get("timeout", 5.0)
        elif "host" in params and "tcp_port" in params:
            legacy["kind"] = "tcp"
            legacy["host"] = params.get("host", "")
            legacy["port"] = params.get("tcp_port", 0)
            legacy["timeout_seconds"] = params.get("timeout", 5.0)

        if legacy:
            if "protocol" in params:
                legacy["protocol"] = params["protocol"]
            return legacy

        raise ValueError(f"[{self.name}] missing 'io' configuration block.")

    def _resolve_protocol(
        self,
        params: Mapping[str, Any],
        io_cfg: Mapping[str, Any],
    ) -> str:
        """Resolve protocol from ``io.protocol`` with driver-based defaults."""
        driver = str(params.get("driver", "")).strip().lower()
        instrument_name = str(getattr(self, "name", "")).strip().lower()

        default = (
            "acoem"
            if driver == "ne300" or instrument_name == "ne300"
            else "aurora"
        )

        # Top-level protocol is retained only for backward compatibility.
        raw = io_cfg.get("protocol", params.get("protocol", default))
        protocol = self._as_text(raw, name="io.protocol").lower()

        if protocol not in {"aurora", "acoem"}:
            raise ValueError(
                f"[{self.name}] unsupported io.protocol={protocol!r}; "
                "expected 'aurora' or 'acoem'."
            )
        return protocol

    def _optional_mapping(
        self,
        payload: Mapping[str, Any],
        key: str,
    ) -> dict[str, Any]:
        value = payload.get(key)
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise ValueError(
                f"[{self.name}] invalid '{key}' configuration block; "
                "expected a mapping."
            )
        return dict(value)

    def _io_kind(
        self,
        io_cfg: Mapping[str, Any],
        *,
        default: str,
    ) -> str:
        text = self._as_text(
            io_cfg.get("kind", io_cfg.get("type", default)),
            name="io.kind",
        ).lower()
        if text in {"tcpip", "socket", "network", "tcp"}:
            return "tcp"
        if text in {"serial", "rs232", "rs485", "usb"}:
            return "serial"
        raise ValueError(f"[{self.name}] unsupported io.kind={text!r}")

    def _build_aggregator(
        self,
        schedule_cfg: Mapping[str, Any],
        processing_cfg: Mapping[str, Any],
        aggregate_cfg: Mapping[str, Any],
    ) -> TimeBucketAggregator | None:
        period_seconds = self._aggregation_period_seconds(
            schedule_cfg,
            processing_cfg,
            aggregate_cfg,
        )
        if period_seconds <= 0:
            return None

        aggregate_setting = processing_cfg.get("aggregate", True)
        if isinstance(aggregate_setting, Mapping):
            enabled_default = aggregate_setting.get("enabled", True)
        else:
            enabled_default = aggregate_setting

        enabled = self._as_bool(
            aggregate_cfg.get("enabled", enabled_default)
        )
        if not enabled:
            return None

        timestamp = self._as_text(
            aggregate_cfg.get(
                "timestamp",
                processing_cfg.get(
                    "aggregation_timestamp",
                    schedule_cfg.get("aggregation_timestamp", "end"),
                ),
            ),
            name="aggregation timestamp",
        ).lower()

        default_method = self._as_text(
            aggregate_cfg.get(
                "method",
                processing_cfg.get("aggregation_method", "mean"),
            ),
            name="aggregation method",
        ).lower()

        kwargs: dict[str, Any] = {
            "period_seconds": period_seconds,
            "datetime_field": "dtm",
            "timestamp": timestamp,
            "default_method": default_method,
            "logger": self.logger,
        }

        # Maintain compatibility with TimeBucketAggregator revisions that have
        # or do not have explicit fields / field_methods parameters.
        signature = inspect.signature(TimeBucketAggregator)
        parameters = signature.parameters
        if "fields" in parameters:
            kwargs["fields"] = [
                field for field in self.HEADERS if field != "dtm"
            ]
        if "field_methods" in parameters:
            kwargs["field_methods"] = {}

        return TimeBucketAggregator(**kwargs)

    def _aggregation_period_seconds(
        self,
        schedule_cfg: Mapping[str, Any],
        processing_cfg: Mapping[str, Any],
        aggregate_cfg: Mapping[str, Any],
    ) -> int:
        raw_seconds = aggregate_cfg.get(
            "interval_seconds",
            processing_cfg.get(
                "aggregation_period_seconds",
                schedule_cfg.get("aggregation_period_seconds"),
            ),
        )
        raw_minutes = aggregate_cfg.get(
            "interval_minutes",
            processing_cfg.get(
                "aggregation_period_minutes",
                schedule_cfg.get("aggregation_period_minutes"),
            ),
        )

        if raw_seconds is not None:
            return self._as_int(
                raw_seconds,
                name="aggregation_period_seconds",
            )

        if raw_minutes is not None:
            minutes = self._as_decimal(
                raw_minutes,
                name="aggregation_period_minutes",
            )
            return int(minutes * Decimal(60))

        # Backward-compatible convenience: sub-minute acquisition defaults to
        # one-minute output aggregation when no explicit period is provided.
        sample_every = schedule_cfg.get("sample_every_seconds")
        if sample_every is not None:
            sample_seconds = self._as_decimal(
                sample_every,
                name="sample_every_seconds",
            )
            if Decimal(0) < sample_seconds < Decimal(60):
                return 60

        return 0

    def _resolve_acoem_parameters(
        self,
        params: Mapping[str, Any],
        processing_cfg: Mapping[str, Any],
    ) -> list[int]:
        raw = processing_cfg.get(
            "current_parameters",
            processing_cfg.get(
                "parameters",
                params.get(
                    "current_parameters",
                    params.get(
                        "parameters",
                        self.ACOEM_CURRENT_PARAMETER_IDS,
                    ),
                ),
            ),
        )

        if not isinstance(raw, Iterable) or isinstance(raw, (str, bytes)):
            raise ValueError(
                f"[{self.name}] ACOEM current parameters must be a list "
                "of integers."
            )

        parameters = [
            self._as_int(value, name="acoem parameter") for value in raw
        ]
        if not parameters:
            raise ValueError(
                f"[{self.name}] ACOEM current parameter list is empty."
            )
        return parameters

    # ------------------------------------------------------------------
    # Aurora ASCII protocol
    # ------------------------------------------------------------------

    def _get_aurora_current_sample(self) -> dict[str, Any]:
        response = self._aurora_request(f"VI{self.serial_id}99")
        return self._parse_aurora_vi99(response)

    def _aurora_request(self, command: str) -> str:
        if self.comms is None:
            raise RuntimeError(
                f"[{self.name}] Aurora line communications are not initialized."
            )
        return self.comms.request(command).replace("\r\n\n", "\r\n").strip()

    def _parse_aurora_vi99(self, response: str) -> dict[str, Any]:
        line = self._last_data_line(response)
        if not line:
            raise ValueError(
                f"[{self.name}] Aurora VI099 returned an empty response."
            )

        parts = [
            part.strip()
            for part in line.replace(", ", ",").split(",")
        ]
        expected = 1 + len(self.AURORA_VI99_VALUE_FIELDS)
        if len(parts) < expected:
            raise ValueError(
                f"[{self.name}] Aurora VI099 returned {len(parts)} fields; "
                f"expected at least {expected}: {line!r}"
            )

        record: dict[str, Any] = {
            "dtm": self._parse_datetime(parts[0])
        }
        for field, token in zip(
            self.AURORA_VI99_VALUE_FIELDS,
            parts[1:expected],
        ):
            if field == "DIO_state":
                record[field] = Decimal(self._parse_hex_or_int(token))
            else:
                record[field] = self._as_decimal(token, name=field)
        return record

    @staticmethod
    def _last_data_line(response: str) -> str:
        lines = [
            line.strip()
            for line in response.replace("\r", "\n").split("\n")
        ]
        useful = [line for line in lines if line]
        return useful[-1] if useful else ""

    @staticmethod
    def _parse_hex_or_int(token: str) -> int:
        text = token.strip()
        if not text:
            return 0
        try:
            if text.lower().startswith("0x") or any(
                character in text.upper() for character in "ABCDEF"
            ):
                return int(text, 16)
            if len(text) > 1 and text.isdigit() and text.startswith("0"):
                return int(text, 16)
            return int(Decimal(text))
        except (InvalidOperation, ValueError):
            return int(text, 16)

    # ------------------------------------------------------------------
    # ACOEM binary protocol, current-data subset
    # ------------------------------------------------------------------

    def _init_acoem_tcp(self, io_cfg: Mapping[str, Any]) -> None:
        if self._io_kind(io_cfg, default="tcp") != "tcp":
            raise ValueError(
                f"[{self.name}] io.protocol='acoem' requires io.kind='tcp'."
            )

        self.tcp_host = self._as_text(
            io_cfg.get("host", io_cfg.get("ip", "")),
            name="io.host",
        )
        self.tcp_port = self._as_int(
            io_cfg.get("port", io_cfg.get("port_tcp", 0)),
            name="io.port",
        )
        self.tcp_timeout = self._as_decimal(
            io_cfg.get(
                "timeout_seconds",
                io_cfg.get("timeout", 5.0),
            ),
            name="io.timeout_seconds",
        )
        self.tcp_read_max_bytes = self._as_int(
            io_cfg.get("read_max_bytes", 65536),
            name="io.read_max_bytes",
        )

        if not self.tcp_host or self.tcp_port <= 0:
            raise ValueError(
                f"[{self.name}] ACOEM TCP transport requires "
                "io.host and io.port."
            )
        if self.tcp_read_max_bytes < 8:
            raise ValueError(
                f"[{self.name}] io.read_max_bytes must be at least 8."
            )

    def _get_acoem_current_sample(self) -> dict[str, Any]:
        values = self.get_values(self.acoem_parameters)
        if not values:
            return {}

        record: dict[str, Any] = {}
        for parameter, field in self.ACOEM_PARAMETER_TO_FIELD.items():
            if parameter in values:
                record[field] = values[parameter]

        record.setdefault(
            "dtm",
            datetime.now(timezone.utc).replace(microsecond=0),
        )
        return record

    def get_instr_type(self) -> list[int]:
        if self.protocol != "acoem":
            return []
        response = self._tcp_request(
            self._acoem_construct_message(command=1),
            end_marker=b"\x04",
        )
        return self._acoem_bytes_to_ints(response, expected_command=1)

    def get_version(self) -> list[int]:
        if self.protocol != "acoem":
            return []
        response = self._tcp_request(
            self._acoem_construct_message(command=2),
            end_marker=b"\x04",
        )
        return self._acoem_bytes_to_ints(response, expected_command=2)

    def get_values(
        self,
        parameters: Iterable[int],
    ) -> dict[int, Any]:
        params = [
            self._as_int(parameter, name="acoem parameter")
            for parameter in parameters
        ]
        if not params:
            return {}

        payload = b"".join(
            parameter.to_bytes(4, byteorder="big", signed=False)
            for parameter in params
        )
        response = self._tcp_request(
            self._acoem_construct_message(command=4, payload=payload),
            end_marker=b"\x04",
        )
        return self._acoem_response_to_values(params, response)

    def _tcp_request(
        self,
        payload: bytes,
        *,
        end_marker: bytes,
    ) -> bytes:
        """Send one request and read one complete length-delimited ACOEM frame.

        ``end_marker`` is retained in the signature for compatibility, but the
        reader does not stop merely because byte 0x04 occurs in the payload.
        The declared ACOEM message length determines when the frame is complete.
        """
        del end_marker

        chunks: list[bytes] = []
        timeout = self._decimal_to_float(self.tcp_timeout)

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((self.tcp_host, self.tcp_port))
            sock.sendall(payload)

            while True:
                raw = b"".join(chunks)
                data = raw.replace(self._TELNET_NEGOTIATION, b"")

                if len(data) >= 6:
                    message_length = int.from_bytes(
                        data[4:6],
                        byteorder="big",
                    )
                    expected_length = 6 + message_length + 2
                    if len(data) >= expected_length:
                        return data[:expected_length]

                if len(data) >= self.tcp_read_max_bytes:
                    raise ValueError(
                        f"[{self.name}] ACOEM response exceeds "
                        f"io.read_max_bytes={self.tcp_read_max_bytes}."
                    )

                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)

        data = b"".join(chunks).replace(self._TELNET_NEGOTIATION, b"")
        if not data:
            raise ConnectionError(
                f"[{self.name}] ACOEM instrument closed the connection "
                "without returning data."
            )

        if len(data) < 6:
            raise ValueError(
                f"[{self.name}] truncated ACOEM response: {data!r}"
            )

        message_length = int.from_bytes(data[4:6], byteorder="big")
        expected_length = 6 + message_length + 2
        raise ValueError(
            f"[{self.name}] truncated ACOEM response: received "
            f"{len(data)} bytes, expected {expected_length}."
        )

    def _acoem_construct_message(
        self,
        *,
        command: int,
        parameter_id: int = 0,
        payload: bytes = b"",
    ) -> bytes:
        msg_data = b""
        if parameter_id > 0:
            msg_data += parameter_id.to_bytes(
                4,
                byteorder="big",
                signed=False,
            )
        msg_data += payload

        msg = (
            bytes([2, self.serial_id, command, 3])
            + len(msg_data).to_bytes(2, byteorder="big")
            + msg_data
        )
        return msg + self._acoem_checksum(msg) + bytes([4])

    @staticmethod
    def _acoem_checksum(data: bytes) -> bytes:
        checksum = 0
        for byte in data:
            checksum ^= byte
        return bytes([checksum])

    def _acoem_response_body(
        self,
        response: bytes,
        *,
        expected_command: int | None = None,
    ) -> tuple[int, bytes]:
        """Validate an ACOEM response and return ``(command, body)``."""
        response = response.replace(self._TELNET_NEGOTIATION, b"")
        if len(response) < 8:
            raise ValueError(
                f"[{self.name}] ACOEM response is too short: {response!r}"
            )
        if response[0] != 2 or response[3] != 3:
            raise ValueError(
                f"[{self.name}] malformed ACOEM frame header: "
                f"{response[:6]!r}"
            )

        message_length = int.from_bytes(response[4:6], byteorder="big")
        frame_length = 6 + message_length + 2
        if len(response) < frame_length:
            raise ValueError(
                f"[{self.name}] truncated ACOEM frame: received "
                f"{len(response)} bytes, expected {frame_length}."
            )

        frame = response[:frame_length]
        if frame[-1] != 4:
            raise ValueError(
                f"[{self.name}] malformed ACOEM frame: missing EOT."
            )

        expected_checksum = self._acoem_checksum(frame[:-2])
        received_checksum = frame[-2:-1]
        if received_checksum != expected_checksum:
            raise ValueError(
                f"[{self.name}] ACOEM checksum mismatch: "
                f"received={received_checksum.hex()} "
                f"expected={expected_checksum.hex()}."
            )

        command = frame[2]
        body = frame[6 : 6 + message_length]

        if command == 0:
            error_code = body[0] if body else None
            self.logger.error(
                "[%s] ACOEM error response code=%s frame=%r",
                self.name,
                error_code,
                frame,
            )
            return command, body

        if expected_command is not None and command != expected_command:
            raise ValueError(
                f"[{self.name}] unexpected ACOEM command in response: "
                f"received={command}, expected={expected_command}."
            )

        return command, body

    def _acoem_bytes_to_ints(
        self,
        response: bytes,
        *,
        expected_command: int | None = None,
    ) -> list[int]:
        command, body = self._acoem_response_body(
            response,
            expected_command=expected_command,
        )
        if command == 0:
            return []
        if len(body) % 4 != 0:
            raise ValueError(
                f"[{self.name}] ACOEM integer response body has "
                f"{len(body)} bytes; expected a multiple of four."
            )
        return [
            int.from_bytes(body[index : index + 4], byteorder="big")
            for index in range(0, len(body), 4)
        ]

    def _acoem_response_to_values(
        self,
        parameters: list[int],
        response: bytes,
    ) -> dict[int, Any]:
        command, body = self._acoem_response_body(
            response,
            expected_command=4,
        )
        if command == 0:
            return {}
        if len(body) % 4 != 0:
            raise ValueError(
                f"[{self.name}] ACOEM value response body has "
                f"{len(body)} bytes; expected a multiple of four."
            )

        chunks = [
            body[index : index + 4]
            for index in range(0, len(body), 4)
        ]
        if len(chunks) != len(parameters):
            raise ValueError(
                f"[{self.name}] ACOEM response field count mismatch: "
                f"requested {len(parameters)}, received {len(chunks)}."
            )

        result: dict[int, Any] = {}
        for parameter, chunk in zip(parameters, chunks):
            if parameter in {1, 2201}:
                result[parameter] = self._acoem_timestamp_to_datetime(
                    int.from_bytes(chunk, "big")
                )
            elif self._acoem_parameter_is_int(parameter):
                result[parameter] = Decimal(struct.unpack(">i", chunk)[0])
            else:
                result[parameter] = Decimal(
                    str(struct.unpack(">f", chunk)[0])
                )
        return result

    @staticmethod
    def _acoem_timestamp_to_datetime(timestamp: int) -> datetime:
        second = timestamp % 64
        timestamp //= 64
        minute = timestamp % 64
        timestamp //= 64
        hour = timestamp % 32
        timestamp //= 32
        day = timestamp % 32
        timestamp //= 32
        month = timestamp % 16
        year = timestamp // 16 + 2000
        return datetime(
            year,
            month,
            day,
            hour,
            minute,
            second,
            tzinfo=timezone.utc,
        )

    @staticmethod
    def _acoem_parameter_is_int(parameter: int) -> bool:
        return (
            1000 < parameter < 5000
            or 12000000 < parameter < 13000000
            or 14000000 < parameter < 15000000
            or 16000000 < parameter < 17000000
            or 27000000 < parameter < 2027000000
        )

    # ------------------------------------------------------------------
    # Formatting and typed conversion helpers
    # ------------------------------------------------------------------

    def _format_record(
        self,
        record: Mapping[str, Any],
    ) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for field in self.HEADERS:
            value: object = record.get(field, "")
            if field == "dtm":
                output[field] = self._format_datetime(value, separator="T")
            elif value is None or value == "":
                output[field] = ""
            else:
                output[field] = self._format_decimal_3(
                    value,
                    name=field,
                )
        return output

    @staticmethod
    def _format_datetime(
        value: object,
        *,
        separator: str = "T",
    ) -> str:
        if isinstance(value, datetime):
            dtm = value
        elif isinstance(value, str):
            dtm = NEPH._parse_datetime(value)
        else:
            raise TypeError(f"Invalid datetime value: {value!r}")

        return dtm.replace(
            tzinfo=None,
            microsecond=0,
        ).isoformat(
            timespec="seconds",
            sep=separator,
        )

    @staticmethod
    def _parse_datetime(value: str) -> datetime:
        text = value.strip().replace("T", " ")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")

    @classmethod
    def _format_decimal_3(
        cls,
        value: object,
        *,
        name: str,
    ) -> str:
        decimal_value = cls._as_decimal(value, name=name)
        rounded = decimal_value.quantize(Decimal("0.001"))
        return f"{rounded:.3f}"

    @staticmethod
    def _as_decimal(value: object, *, name: str) -> Decimal:
        if value is None or value == "":
            raise ValueError(f"Missing numeric value for {name}.")
        if isinstance(value, bool):
            raise TypeError(
                f"Boolean is not a valid numeric value for {name}."
            )
        if isinstance(value, Decimal):
            return value
        if isinstance(value, int):
            return Decimal(value)
        if isinstance(value, float):
            return Decimal(str(value))
        if isinstance(value, str):
            text = value.strip()
            if not text:
                raise ValueError(f"Missing numeric value for {name}.")
            try:
                return Decimal(text)
            except InvalidOperation as exc:
                raise ValueError(
                    f"Invalid numeric value for {name}: {value!r}"
                ) from exc
        raise TypeError(f"Invalid numeric value for {name}: {value!r}")

    @classmethod
    def _as_int(cls, value: object, *, name: str) -> int:
        return int(cls._as_decimal(value, name=name))

    @staticmethod
    def _as_text(value: object, *, name: str) -> str:
        if value is None:
            raise ValueError(f"Missing text value for {name}.")
        text = str(value).strip()
        if not text:
            raise ValueError(f"Empty text value for {name}.")
        return text

    @staticmethod
    def _as_bool(value: object) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
                "y",
            }
        return bool(value)

    @staticmethod
    def _decimal_to_float(value: Decimal) -> float:
        return float(value)
class NE300(NEPH):
    """ACOEM NE-300 driver using the instrument's internal data logger.

    Unlike :class:`NEPH`, which polls a single current-value record, this class
    uses ACOEM command 7 to retrieve every one-minute record stored by the
    instrument.  Each command-7 response contains a header record with the
    actual parameter IDs followed by one or more data records.

    The writer header is updated from the command-6 configuration and, more
    importantly, from the command-7 header before the first row is written.
    This preserves the complete record and avoids hard-coding a reduced set of
    current-value fields.
    """

    # A non-empty bootstrap header is required so the orchestrator creates an
    # HourlyCsvWriter.  It is replaced during initialize() and again from the
    # first command-7 header before any data are appended.
    HEADERS = ["dtm"]

    # Header observed in the MKN records collected on 2026-08-01.  It is used
    # only as a safe startup fallback when command 6 is unavailable.  The
    # command-7 packet header remains authoritative.
    DEFAULT_LOGGED_PARAMETER_IDS = [
        2635000,
        2525000,
        2450000,
        2635090,
        2525090,
        2450090,
        5001,
        5002,
        5003,
        5004,
        5005,
        5006,
        5010,
        26635000,
        26525000,
        26450000,
        13525000,
        15635000,
        15525000,
        15450000,
        11635000,
        11525000,
        11450000,
        11635090,
        11525090,
        11450090,
        6007,
        6008,
        6001,
        6002,
        6003,
        6635000,
        6525000,
        6450000,
        6635090,
        6525090,
        6450090,
    ]

    def initialize(self) -> None:
        """Initialize ACOEM communications and logged-data retrieval."""
        super().initialize()

        if self.protocol != "acoem":
            raise ValueError(
                f"[{self.name}] NE300 requires io.protocol='acoem'."
            )

        # The NE-300 already stores one-minute aggregates internally.  Do not
        # aggregate the retrieved logger records a second time in pydaq.
        self.aggregator = None
        self.empty_record_is_ok = True

        params = self._params()
        processing_cfg = self._optional_mapping(params, "processing")
        data_log_value = processing_cfg.get("data_log", {})
        if data_log_value is None:
            data_log_cfg: dict[str, Any] = {}
        elif isinstance(data_log_value, Mapping):
            data_log_cfg = dict(data_log_value)
        else:
            raise ValueError(
                f"[{self.name}] processing.data_log must be a mapping."
            )

        self.logged_chunk_seconds = max(
            60,
            self._as_int(
                data_log_cfg.get("chunk_seconds", 3600),
                name="processing.data_log.chunk_seconds",
            ),
        )
        self.logged_overlap_seconds = max(
            0,
            self._as_int(
                data_log_cfg.get("overlap_seconds", 60),
                name="processing.data_log.overlap_seconds",
            ),
        )

        configured_parameters = data_log_cfg.get(
            "parameters",
            self.DEFAULT_LOGGED_PARAMETER_IDS,
        )
        self._set_logged_parameter_ids(
            configured_parameters,
            source="configuration/default",
        )

        now = self._floor_to_minute(datetime.now(timezone.utc))
        self._logged_cursor = now
        self._last_logged_dtm: datetime | None = None

        # Command 6 is useful but not fully reliable on all firmware versions.
        # Keep the fallback above and let the first command-7 packet header
        # override it before rows are written.
        try:
            reported = self.get_data_log_config()
            if reported:
                self._set_logged_parameter_ids(
                    reported,
                    source="command 6 data-log config",
                )
            else:
                self.logger.warning(
                    "[%s] command 6 returned no logged parameter IDs; "
                    "using fallback until a command-7 header is received",
                    self.name,
                )
        except Exception as exc:
            self.logger.warning(
                "[%s] could not read command-6 data-log config: %s; "
                "using fallback until a command-7 header is received",
                self.name,
                exc,
            )

        self.logger.info(
            "[%s] NE300 logged-data retrieval ready fields=%d "
            "chunk_seconds=%d overlap_seconds=%d",
            self.name,
            len(self.logged_parameter_ids),
            self.logged_chunk_seconds,
            self.logged_overlap_seconds,
        )

    def append_record(self) -> None:
        """Retrieve and append every newly logged NE-300 record."""
        with self._state_lock:
            if not self.state.enabled:
                return

        end = self._floor_to_minute(datetime.now(timezone.utc))
        cursor = self._logged_cursor
        if end <= cursor:
            return

        written: list[dict[str, Any]] = []

        while cursor < end:
            chunk_end = min(
                cursor + timedelta(seconds=self.logged_chunk_seconds),
                end,
            )
            query_start = cursor
            if self._last_logged_dtm is not None and self.logged_overlap_seconds:
                query_start = max(
                    self._last_logged_dtm
                    - timedelta(seconds=self.logged_overlap_seconds),
                    datetime(2000, 1, 1, tzinfo=timezone.utc),
                )

            records = self.get_logged_data(query_start, chunk_end)

            for record in sorted(
                records,
                key=lambda item: self._coerce_logged_datetime(
                    item.get("dtm")
                ),
            ):
                dtm = self._coerce_logged_datetime(record.get("dtm"))

                # Treat command-7 end times as exclusive.  The next query starts
                # at the previous end, so this also prevents boundary doubles.
                if dtm < query_start or dtm >= chunk_end:
                    continue
                if (
                    self._last_logged_dtm is not None
                    and dtm <= self._last_logged_dtm
                ):
                    continue

                formatted = self._format_logged_record(record)
                if self.writer:
                    self.writer.append(formatted)
                written.append(formatted)
                self._last_logged_dtm = dtm

            # Advance only after this chunk completed without an exception.
            self._logged_cursor = chunk_end
            cursor = chunk_end

        if not written:
            self.logger.debug(
                "[%s] no new logged records available for %s to %s",
                self.name,
                self._logged_cursor.isoformat(),
                end.isoformat(),
            )
            if self.writer:
                self.writer.finalize_if_needed(now=end)
            return

        latest = written[-1]
        with self._state_lock:
            previous_empty = self._consecutive_empty_records
            self._consecutive_empty_records = 0
            self.state.latest = latest
            self.state.last_sample_ts = time.time()
            self.state.last_error = ""

        if previous_empty:
            self.logger.info(
                "recovered after %s empty acquisition cycle(s)",
                previous_empty,
            )

        if self.writer:
            self.writer.finalize_if_needed(now=end)

        self.logger.debug(
            "[%s] appended %d logged record(s), latest=%s",
            self.name,
            len(written),
            latest.get("dtm"),
        )

    def get_data_log_config(self) -> list[int]:
        """Return parameter IDs configured in the NE-300 data logger."""
        response = self._tcp_request(
            self._acoem_construct_message(command=6),
            end_marker=b"\x04",
        )
        values = self._acoem_bytes_to_ints(
            response,
            expected_command=6,
        )
        if not values:
            return []

        reported_count = values[0]
        parameters = values[1:]
        if reported_count != len(parameters):
            self.logger.warning(
                "[%s] command-6 field count mismatch: "
                "reported=%d received=%d; using received IDs",
                self.name,
                reported_count,
                len(parameters),
            )
        return self._normalise_logged_parameter_ids(parameters)

    def get_logged_data(
        self,
        start: datetime,
        end: datetime,
    ) -> list[dict[Any, Any]]:
        """Retrieve all logged records in ``[start, end)`` using command 7."""
        start_utc = self._as_utc_datetime(start)
        end_utc = self._as_utc_datetime(end)
        if end_utc <= start_utc:
            return []

        payload = (
            self._acoem_datetime_to_timestamp(start_utc)
            + self._acoem_datetime_to_timestamp(end_utc)
        )
        response = self._tcp_request(
            self._acoem_construct_message(command=7, payload=payload),
            end_marker=b"\x04",
        )
        return self._decode_logged_data_response(response)

    def _decode_logged_data_response(
        self,
        response: bytes,
    ) -> list[dict[Any, Any]]:
        """Decode a complete command-7 response, including its header record."""
        command, body = self._acoem_response_body(
            response,
            expected_command=7,
        )
        if command == 0:
            return []

        result: list[dict[Any, Any]] = []
        packet_parameters: list[int] | None = None
        offset = 0

        while offset < len(body):
            if offset + 16 > len(body):
                raise ValueError(
                    f"[{self.name}] truncated command-7 record header "
                    f"at body offset {offset}."
                )

            record_type = body[offset]
            current_operation = body[offset + 1]
            timestamp_raw = int.from_bytes(
                body[offset + 4 : offset + 8],
                byteorder="big",
            )
            logging_period = int.from_bytes(
                body[offset + 8 : offset + 12],
                byteorder="big",
            )
            field_count = int.from_bytes(
                body[offset + 12 : offset + 16],
                byteorder="big",
            )
            record_length = 16 + 4 * field_count
            if offset + record_length > len(body):
                raise ValueError(
                    f"[{self.name}] truncated command-7 record at "
                    f"body offset {offset}: need {record_length} bytes, "
                    f"have {len(body) - offset}."
                )

            fields = [
                body[
                    offset + 16 + index * 4 :
                    offset + 20 + index * 4
                ]
                for index in range(field_count)
            ]

            if record_type == 1:
                packet_parameters = [
                    int.from_bytes(field, byteorder="big")
                    for field in fields
                ]
                packet_parameters = self._normalise_logged_parameter_ids(
                    packet_parameters
                )
                self._set_logged_parameter_ids(
                    packet_parameters,
                    source="command 7 packet header",
                )

            elif record_type == 0:
                if packet_parameters is None:
                    raise ValueError(
                        f"[{self.name}] command-7 data record arrived "
                        "before a parameter header."
                    )
                if len(fields) != len(packet_parameters):
                    raise ValueError(
                        f"[{self.name}] command-7 field count mismatch: "
                        f"header={len(packet_parameters)} "
                        f"record={len(fields)}."
                    )

                record: dict[Any, Any] = {
                    "dtm": self._acoem_timestamp_to_datetime(
                        timestamp_raw
                    ),
                    4035: int(current_operation),
                    2002: int(logging_period),
                }
                for parameter, field in zip(packet_parameters, fields):
                    record[parameter] = round(
                        struct.unpack(">f", field)[0],
                        5,
                    )
                result.append(record)

            else:
                self.logger.warning(
                    "[%s] ignoring unsupported command-7 record_type=%d",
                    self.name,
                    record_type,
                )

            offset += record_length

        return result

    def _set_logged_parameter_ids(
        self,
        parameter_ids: Iterable[Any],
        *,
        source: str,
    ) -> None:
        """Set the complete CSV header before logged rows are written."""
        parameters = self._normalise_logged_parameter_ids(parameter_ids)
        if not parameters:
            raise ValueError(
                f"[{self.name}] no valid NE300 logged parameter IDs "
                f"received from {source}."
            )

        headers = ["dtm", "4035", "2002"] + [
            str(parameter) for parameter in parameters
        ]
        current = getattr(self, "logged_parameter_ids", None)
        if current == parameters:
            return

        writer_is_open = bool(
            self.writer
            and getattr(self.writer, "_file_handle", None) is not None
        )
        if writer_is_open:
            raise RuntimeError(
                f"[{self.name}] NE300 logged parameter header changed "
                "while a CSV file was open; refusing to mix schemas. "
                f"source={source} old={current} new={parameters}"
            )

        self.logged_parameter_ids = parameters
        self.HEADERS = headers
        if self.writer:
            self.writer.headers = list(headers)

        self.logger.info(
            "[%s] NE300 data header set from %s: %d columns",
            self.name,
            source,
            len(headers),
        )

    @staticmethod
    def _normalise_logged_parameter_ids(
        parameter_ids: Iterable[Any],
    ) -> list[int]:
        result: list[int] = []
        seen: set[int] = set()
        for value in parameter_ids:
            parameter = int(value)
            if parameter <= 0 or parameter in {4035, 2002}:
                continue
            if parameter in seen:
                continue
            seen.add(parameter)
            result.append(parameter)
        return result

    def _format_logged_record(
        self,
        record: Mapping[Any, Any],
    ) -> dict[str, Any]:
        """Format one complete logged record in the active packet-header order."""
        dtm = self._coerce_logged_datetime(record.get("dtm"))
        output: dict[str, Any] = {
            "dtm": dtm.astimezone(timezone.utc)
            .replace(tzinfo=None, microsecond=0)
            .strftime("%Y-%m-%d %H:%M:%S"),
            "4035": int(record.get(4035, 0)),
            "2002": int(record.get(2002, 0)),
        }

        for parameter in self.logged_parameter_ids:
            value = record.get(parameter)
            output[str(parameter)] = "" if value is None else value
        return output

    @classmethod
    def _coerce_logged_datetime(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return cls._as_utc_datetime(value)
        if isinstance(value, str):
            return cls._as_utc_datetime(cls._parse_datetime(value))
        raise ValueError(f"Invalid NE300 logged timestamp: {value!r}")

    @staticmethod
    def _as_utc_datetime(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @classmethod
    def _floor_to_minute(cls, value: datetime) -> datetime:
        return cls._as_utc_datetime(value).replace(second=0, microsecond=0)

    @staticmethod
    def _acoem_datetime_to_timestamp(value: datetime) -> bytes:
        """Encode a UTC datetime using the ACOEM packed timestamp format."""
        dtm = value.astimezone(timezone.utc).replace(microsecond=0)
        year = dtm.year - 2000
        if not 0 <= year <= 255:
            raise ValueError(
                f"ACOEM timestamp year out of range: {dtm.year}"
            )

        packed = year
        packed = packed * 16 + dtm.month
        packed = packed * 32 + dtm.day
        packed = packed * 32 + dtm.hour
        packed = packed * 64 + dtm.minute
        packed = packed * 64 + dtm.second
        return packed.to_bytes(4, byteorder="big", signed=False)
