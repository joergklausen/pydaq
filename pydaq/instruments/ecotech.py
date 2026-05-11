from __future__ import annotations

"""Ecotech / ACOEM nephelometer driver for pydaq.

This driver covers the nephelometer family while keeping aggregation in the
shared :class:`TimeBucketAggregator` from ``instrument.py``:

* ``protocol: aurora`` for the Ecotech Aurora 3000 ASCII protocol.
* ``protocol: acoem`` for the newer ACOEM binary protocol used by the NE-300.

The driver reads one instantaneous sample from the instrument. If the YAML
configuration requests aggregation, the sample is passed to
``TimeBucketAggregator`` and ``get_record()`` returns ``{}`` until a completed
bucket is available. The pydaq ``Instrument`` base class should set
``empty_record_is_ok`` to avoid treating this as an acquisition error.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import inspect
import socket
import struct
from typing import Any, Iterable, Mapping

from pydaq.instruments.instrument import Instrument, LineComms, TimeBucketAggregator


class NEPH(Instrument):
    """Driver for Aurora 3000 and NE-300 nephelometers.

    The output columns intentionally preserve the historical Aurora 3000 format
    used by nrbdaq. For the ACOEM protocol, equivalent current-value parameter
    IDs are mapped onto the same column names.
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

    # Equivalent to the Aurora VI099 current-data response, as documented in
    # the previous mkndaq/neph.py implementation.
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

    def initialize(self) -> None:
        """Read driver parameters and initialize communication helpers."""
        params = self._params()
        io_cfg = self._resolve_io_config(params)
        schedule_cfg = self._optional_mapping(params, "schedule")
        processing_cfg = self._optional_mapping(params, "processing")
        aggregate_cfg = self._optional_mapping(params, "aggregate")
        init_cfg = self._optional_mapping(params, "init")

        self.protocol = self._as_text(params.get("protocol", "aurora"), name="protocol").lower()
        if self.protocol not in {"aurora", "acoem"}:
            raise ValueError(
                f"[{self.name}] unsupported Ecotech protocol {self.protocol!r}; "
                "expected 'aurora' or 'acoem'."
            )

        self.serial_id = self._as_int(params.get("serial_id", params.get("id", 0)), name="serial_id")
        self.io_kind = self._io_kind(io_cfg, default="serial" if self.protocol == "aurora" else "tcp")
        self.comms: LineComms | None = None

        if self.protocol == "aurora":
            self.comms = LineComms(dict(io_cfg), logger=self.logger)
        else:
            self._init_acoem_tcp(io_cfg)

        self.acoem_parameters = self._resolve_acoem_parameters(params)
        self.aggregator = self._build_aggregator(schedule_cfg, processing_cfg, aggregate_cfg)
        self.empty_record_is_ok = self.aggregator is not None

        self.logger.info(
            "[%s] initialized Ecotech/ACOEM driver protocol=%s io=%s serial_id=%s aggregation=%s",
            self.name,
            self.protocol,
            self.io_kind,
            self.serial_id,
            "on" if self.aggregator else "off",
        )

        if self._as_bool(init_cfg.get("id_on_initialize", params.get("id_on_initialize", False))):
            ident = self.get_instrument_id()
            if ident:
                self.logger.info("[%s] instrument id: %r", self.name, ident)
            else:
                self.logger.warning("[%s] instrument id query returned no response", self.name)

    def get_record(self) -> dict[str, Any]:
        """Return one formatted record or ``{}`` if an aggregate is still open."""
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
            self.logger.error("[%s] Ecotech get_record failed: %s", self.name, exc, exc_info=True)
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
        raise ValueError(f"[{self.name}] unsupported protocol {self.protocol!r}")

    def get_instrument_id(self) -> str:
        """Return an instrument identification string if the protocol supports it."""
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
            raise ValueError(f"[{self.name}] missing driver parameters; expected a dict.")
        return value

    def _resolve_io_config(self, params: Mapping[str, Any]) -> dict[str, Any]:
        io_value = params.get("io")
        if isinstance(io_value, Mapping):
            return dict(io_value)

        # Backward compatibility for early draft configs / legacy nrbdaq names.
        legacy: dict[str, Any] = {}
        if "serial_port" in params or "port" in params:
            legacy["kind"] = "serial"
            legacy["port"] = params.get("serial_port", params.get("port", ""))
            legacy["baudrate"] = params.get("serial_baudrate", params.get("baudrate", 19200))
            legacy["timeout_seconds"] = params.get("serial_timeout", params.get("timeout", 2.0))
        elif "socket" in params and isinstance(params.get("socket"), Mapping):
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
            return legacy
        raise ValueError(f"[{self.name}] missing 'io' configuration block.")

    def _optional_mapping(self, payload: Mapping[str, Any], key: str) -> dict[str, Any]:
        value = payload.get(key)
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise ValueError(f"[{self.name}] invalid '{key}' configuration block; expected a mapping.")
        return dict(value)

    def _io_kind(self, io_cfg: Mapping[str, Any], *, default: str) -> str:
        text = self._as_text(io_cfg.get("kind", io_cfg.get("type", default)), name="io.kind").lower()
        if text in {"tcpip", "socket", "network"}:
            return "tcp"
        if text in {"serial", "rs232", "rs485", "usb"}:
            return "serial"
        if text == "tcp":
            return "tcp"
        raise ValueError(f"[{self.name}] unsupported io.kind={text!r}")

    def _build_aggregator(
        self,
        schedule_cfg: Mapping[str, Any],
        processing_cfg: Mapping[str, Any],
        aggregate_cfg: Mapping[str, Any],
    ) -> TimeBucketAggregator | None:
        period_seconds = self._aggregation_period_seconds(schedule_cfg, aggregate_cfg)
        if period_seconds <= 0:
            return None

        enabled = self._as_bool(aggregate_cfg.get("enabled", processing_cfg.get("aggregate", True)))
        if not enabled:
            return None

        timestamp = self._as_text(
            aggregate_cfg.get("timestamp", schedule_cfg.get("aggregation_timestamp", "end")),
            name="aggregation timestamp",
        ).lower()
        default_method = self._as_text(
            aggregate_cfg.get("method", processing_cfg.get("aggregation_method", "mean")),
            name="aggregation method",
        ).lower()

        # Build the aggregator using the constructor supported by the installed
        # instrument.py.  Some pydaq revisions require an explicit ``fields``
        # argument; older temporary revisions did not expose it.  We inspect the
        # signature once at runtime and pass only supported keyword arguments,
        # which avoids Pylance "No parameter named ..." errors without suppressing
        # type checking and keeps this driver compatible across the transition.
        kwargs: dict[str, Any] = {
            "period_seconds": period_seconds,
            "datetime_field": "dtm",
            "timestamp": timestamp,
            "default_method": default_method,
            "logger": self.logger,
        }
        signature = inspect.signature(TimeBucketAggregator)
        parameters = signature.parameters
        if "fields" in parameters:
            kwargs["fields"] = [field for field in self.HEADERS if field != "dtm"]
        if "field_methods" in parameters:
            kwargs["field_methods"] = {}

        return TimeBucketAggregator(**kwargs)

    def _aggregation_period_seconds(
        self,
        schedule_cfg: Mapping[str, Any],
        aggregate_cfg: Mapping[str, Any],
    ) -> int:
        raw_seconds = aggregate_cfg.get("interval_seconds", schedule_cfg.get("aggregation_period_seconds"))
        raw_minutes = aggregate_cfg.get("interval_minutes", schedule_cfg.get("aggregation_period_minutes"))

        if raw_seconds is not None:
            return self._as_int(raw_seconds, name="aggregation_period_seconds")
        if raw_minutes is not None:
            minutes = self._as_decimal(raw_minutes, name="aggregation_period_minutes")
            return int(minutes * Decimal(60))

        sample_every = schedule_cfg.get("sample_every_seconds")
        if sample_every is not None:
            sample_seconds = self._as_decimal(sample_every, name="sample_every_seconds")
            if Decimal(0) < sample_seconds < Decimal(60):
                return 60
        return 0

    def _resolve_acoem_parameters(self, params: Mapping[str, Any]) -> list[int]:
        raw = params.get("parameters", params.get("current_parameters", self.ACOEM_CURRENT_PARAMETER_IDS))
        if not isinstance(raw, Iterable) or isinstance(raw, (str, bytes)):
            raise ValueError(f"[{self.name}] ACOEM parameters must be a list of integers.")
        return [self._as_int(value, name="acoem parameter") for value in raw]

    # ------------------------------------------------------------------
    # Aurora ASCII protocol
    # ------------------------------------------------------------------
    def _get_aurora_current_sample(self) -> dict[str, Any]:
        response = self._aurora_request(f"VI{self.serial_id}99")
        return self._parse_aurora_vi99(response)

    def _aurora_request(self, command: str) -> str:
        if self.comms is None:
            raise RuntimeError(f"[{self.name}] Aurora line communications are not initialized.")
        return self.comms.request(command).replace("\r\n\n", "\r\n").strip()

    def _parse_aurora_vi99(self, response: str) -> dict[str, Any]:
        line = self._last_data_line(response)
        if not line:
            raise ValueError(f"[{self.name}] Aurora VI099 returned an empty response.")

        parts = [part.strip() for part in line.replace(", ", ",").split(",")]
        expected = 1 + len(self.AURORA_VI99_VALUE_FIELDS)
        if len(parts) < expected:
            raise ValueError(
                f"[{self.name}] Aurora VI099 returned {len(parts)} fields; expected at least {expected}: {line!r}"
            )

        record: dict[str, Any] = {"dtm": self._parse_datetime(parts[0])}
        for field, token in zip(self.AURORA_VI99_VALUE_FIELDS, parts[1:expected]):
            if field == "DIO_state":
                record[field] = Decimal(self._parse_hex_or_int(token))
            else:
                record[field] = self._as_decimal(token, name=field)
        return record

    @staticmethod
    def _last_data_line(response: str) -> str:
        lines = [line.strip() for line in response.replace("\r", "\n").split("\n")]
        useful = [line for line in lines if line]
        return useful[-1] if useful else ""

    @staticmethod
    def _parse_hex_or_int(token: str) -> int:
        text = token.strip()
        if not text:
            return 0
        try:
            if text.lower().startswith("0x") or any(ch in text.upper() for ch in "ABCDEF"):
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
            raise ValueError(f"[{self.name}] protocol='acoem' currently requires io.kind='tcp'.")
        self.tcp_host = self._as_text(io_cfg.get("host", io_cfg.get("ip", "")), name="io.host")
        self.tcp_port = self._as_int(io_cfg.get("port", io_cfg.get("port_tcp", 0)), name="io.port")
        self.tcp_timeout = self._as_decimal(io_cfg.get("timeout_seconds", io_cfg.get("timeout", 5.0)), name="io.timeout")
        self.tcp_read_max_bytes = self._as_int(io_cfg.get("read_max_bytes", 65536), name="io.read_max_bytes")
        if not self.tcp_host or self.tcp_port <= 0:
            raise ValueError(f"[{self.name}] ACOEM TCP transport requires io.host and io.port.")

    def _get_acoem_current_sample(self) -> dict[str, Any]:
        values = self.get_values(self.acoem_parameters)
        if not values:
            return {}

        record: dict[str, Any] = {}
        for parameter, field in self.ACOEM_PARAMETER_TO_FIELD.items():
            if parameter in values:
                record[field] = values[parameter]
        record.setdefault("dtm", datetime.now(timezone.utc).replace(microsecond=0))
        return record

    def get_instr_type(self) -> list[int]:
        if self.protocol != "acoem":
            return []
        response = self._tcp_request(self._acoem_construct_message(command=1), end_marker=b"\x04")
        return self._acoem_bytes_to_ints(response)

    def get_version(self) -> list[int]:
        if self.protocol != "acoem":
            return []
        response = self._tcp_request(self._acoem_construct_message(command=2), end_marker=b"\x04")
        return self._acoem_bytes_to_ints(response)

    def get_values(self, parameters: Iterable[int]) -> dict[int, Any]:
        params = [self._as_int(parameter, name="acoem parameter") for parameter in parameters]
        if not params:
            return {}
        payload = b"".join(parameter.to_bytes(4, byteorder="big", signed=False) for parameter in params)
        response = self._tcp_request(self._acoem_construct_message(command=4, payload=payload), end_marker=b"\x04")
        return self._acoem_response_to_values(params, response)

    def _tcp_request(self, payload: bytes, *, end_marker: bytes) -> bytes:
        chunks: list[bytes] = []
        timeout = self._decimal_to_float(self.tcp_timeout)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((self.tcp_host, self.tcp_port))
            sock.sendall(payload)
            while sum(len(chunk) for chunk in chunks) < self.tcp_read_max_bytes:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                data = b"".join(chunks)
                if end_marker in data or data.endswith(end_marker):
                    break
        data = b"".join(chunks).strip()
        return data.replace(b"\xff\xfb\x01\xff\xfe\x01\xff\xfb\x03", b"")

    def _acoem_construct_message(self, *, command: int, parameter_id: int = 0, payload: bytes = b"") -> bytes:
        msg_data = b""
        if parameter_id > 0:
            msg_data += parameter_id.to_bytes(4, byteorder="big", signed=False)
        msg_data += payload
        msg = bytes([2, self.serial_id, command, 3]) + len(msg_data).to_bytes(2, byteorder="big") + msg_data
        return msg + self._acoem_checksum(msg) + bytes([4])

    @staticmethod
    def _acoem_checksum(data: bytes) -> bytes:
        checksum = 0
        for byte in data:
            checksum ^= byte
        return bytes([checksum])

    def _acoem_bytes_to_ints(self, response: bytes) -> list[int]:
        if not response or len(response) < 8:
            return []
        if response[2] == 0:
            self.logger.error("[%s] ACOEM error response: %r", self.name, response)
            return []
        msg_len = int.from_bytes(response[4:6], byteorder="big")
        body = response[6 : 6 + msg_len]
        return [int.from_bytes(body[index : index + 4], byteorder="big") for index in range(0, len(body), 4)]

    def _acoem_response_to_values(self, parameters: list[int], response: bytes) -> dict[int, Any]:
        if not response or len(response) < 8:
            return {}
        if response[2] == 0:
            self.logger.error("[%s] ACOEM error response: %r", self.name, response)
            return {}

        msg_len = int.from_bytes(response[4:6], byteorder="big")
        body = response[6 : 6 + msg_len]
        chunks = [body[index : index + 4] for index in range(0, len(body), 4)]
        if len(chunks) != len(parameters):
            raise ValueError(
                f"[{self.name}] ACOEM response field count mismatch: "
                f"requested {len(parameters)}, received {len(chunks)}."
            )

        result: dict[int, Any] = {}
        for parameter, chunk in zip(parameters, chunks):
            if parameter == 1:
                result[parameter] = self._acoem_timestamp_to_datetime(int.from_bytes(chunk, "big"))
            elif self._acoem_parameter_is_int(parameter):
                result[parameter] = Decimal(struct.unpack(">i", chunk)[0])
            else:
                result[parameter] = Decimal(str(struct.unpack(">f", chunk)[0]))
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
        return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)

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
    def _format_record(self, record: Mapping[str, Any]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for field in self.HEADERS:
            value: object = record.get(field, "")
            if field == "dtm":
                output[field] = self._format_datetime(value, separator="T")
            elif value is None or value == "":
                output[field] = ""
            else:
                output[field] = self._format_decimal_3(value, name=field)
        return output

    @staticmethod
    def _format_datetime(value: object, *, separator: str = "T") -> str:
        if isinstance(value, datetime):
            dtm = value
        elif isinstance(value, str):
            dtm = NEPH._parse_datetime(value)
        else:
            raise TypeError(f"Invalid datetime value: {value!r}")
        return dtm.replace(tzinfo=None, microsecond=0).isoformat(timespec="seconds", sep=separator)

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
    def _format_decimal_3(cls, value: object, *, name: str) -> str:
        decimal_value = cls._as_decimal(value, name=name)
        rounded = decimal_value.quantize(Decimal("0.001"))
        return f"{rounded:.3f}"

    @staticmethod
    def _as_decimal(value: object, *, name: str) -> Decimal:
        if value is None or value == "":
            raise ValueError(f"Missing numeric value for {name}.")
        if isinstance(value, bool):
            raise TypeError(f"Boolean is not a valid numeric value for {name}.")
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
                raise ValueError(f"Invalid numeric value for {name}: {value!r}") from exc
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
            return value.strip().lower() in {"1", "true", "yes", "on", "y"}
        return bool(value)

    @staticmethod
    def _decimal_to_float(value: Decimal) -> float:
        # The socket API requires a native float timeout. This conversion is from
        # a known Decimal attribute, not from Mapping.get()/Any/None.
        return float(value)
