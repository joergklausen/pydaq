from __future__ import annotations

import re
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Any, cast

import serial

from pydaq.instruments.instrument import Instrument


_MEAS_RE = re.compile(
    r"T=\s*(?P<t>[+-]?\d+(?:\.\d+)?)\s*'C"
    r".*?RH=\s*(?P<rh>[+-]?\d+(?:\.\d+)?)\s*%RH"
    r"(?:.*?Td=\s*(?P<td>[+-]?\d+(?:\.\d+)?)\s*'C)?",
    re.IGNORECASE | re.DOTALL,
)


class HMPASCII(Instrument):
    """Driver for Vaisala HMP sensors speaking the ASCII protocol.

    Supported transports:
    - direct serial / serial-to-USB
    - raw TCP serial bridge

    Canonical config example:

        instruments:
          hmp60:
            driver: hmpascii
            id: 5
            io:
              kind: socket   # or serial
              host: 192.168.0.100
              port: 5004
              timeout: 5.0
              idle_timeout: 0.25
              sleep: 0.1
            init:
              command_sequence:
                - "SEND {id}"
    """

    HEADERS = ["dtm", "t", "rh", "td"]

    _serial_by_port: dict[str, serial.Serial] = {}
    _serial_lock_by_port: dict[str, threading.Lock] = {}
    _socket_lock_by_endpoint: dict[str, threading.Lock] = {}

    def initialize(self) -> None:
        """Read and validate orchestrator-supplied parameters."""
        params = self._params()
        io_cfg = self._require_dict(params, "io")
        init_cfg = self._optional_dict(params, "init")

        self.sensor_id: int = self._require_sensor_id(params)
        self.io_kind: str = self._require_str(io_cfg, "kind").lower()
        self.io_sleep: float = float(io_cfg.get("sleep", 0.1))
        self.timeout: float = float(io_cfg.get("timeout", 2.0))
        self.idle_timeout: float = float(io_cfg.get("idle_timeout", min(0.25, self.timeout)))
        self.command_sequence: list[str] = self._resolve_command_sequence(init_cfg)

        self.max_fail_before_cooldown: int = int(init_cfg.get("max_fail_before_cooldown", 5))
        self.cooldown_seconds: float = float(init_cfg.get("cooldown_seconds", 120.0))
        self.fail_count: int = 0
        self.cooldown_until: float = 0.0

        if self.io_kind == "serial":
            self.serial_port: str = self._require_str(io_cfg, "port")
            self._ensure_shared_serial(self.serial_port, io_cfg)
            self.logger.info(
                "[%s] initialized HMPASCII over serial port=%s id=%s",
                self.name,
                self.serial_port,
                self.sensor_id,
            )
            return

        if self.io_kind in {"socket", "tcp"}:
            self.host: str = self._require_str(io_cfg, "host")
            self.socket_port: int = self._require_int(io_cfg, "port")
            endpoint = self._socket_endpoint_key(self.host, self.socket_port)
            if endpoint not in self._socket_lock_by_endpoint:
                self._socket_lock_by_endpoint[endpoint] = threading.Lock()
            self.logger.info(
                "[%s] initialized HMPASCII over socket %s:%s id=%s",
                self.name,
                self.host,
                self.socket_port,
                self.sensor_id,
            )
            return

        raise ValueError(
            f"[{self.name}] unsupported HMPASCII io.kind={self.io_kind!r}; "
            "expected 'serial', 'socket', or 'tcp'"
        )

    def get_record(self) -> dict[str, Any]:
        """Return one parsed measurement record for the pydaq base class."""
        if self.cooldown_until > time.time():
            return {}

        try:
            raw = self._query_measurement()
            parsed = self._extract_measurement(raw)
            if not parsed:
                self._note_failure("no parseable measurement in HMPASCII reply")
                if raw:
                    self.logger.warning("[%s] unparsed reply: %r", self.name, raw)
                return {}

            self.fail_count = 0
            self.cooldown_until = 0.0
            return {
                "dtm": self._now_string(),
                "t": parsed["t"],
                "rh": parsed["rh"],
                "td": parsed.get("td"),
            }
        except Exception as exc:
            self._note_failure(str(exc))
            self.logger.error("[%s] get_record failed: %s", self.name, exc, exc_info=True)
            return {}

    # Backward-compatible alias during transition.
    collect_record = get_record

    def _params(self) -> dict[str, Any]:
        """Return the orchestrator-supplied driver parameters."""
        params = getattr(self, "parameters", None)
        if not isinstance(params, dict):
            raise ValueError(
                f"[{self.name}] missing driver parameters from orchestrator; expected a dict with 'io'."
            )
        return cast(dict[str, Any], params)

    def _require_dict(self, payload: dict[str, Any], key: str) -> dict[str, Any]:
        value = payload.get(key)
        if not isinstance(value, dict):
            raise ValueError(f"[{self.name}] missing or invalid '{key}' configuration block.")
        return cast(dict[str, Any], value)

    def _optional_dict(self, payload: dict[str, Any], key: str) -> dict[str, Any]:
        value = payload.get(key)
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError(f"[{self.name}] invalid '{key}' configuration block; expected a mapping.")
        return cast(dict[str, Any], value)

    def _require_str(self, payload: dict[str, Any], key: str) -> str:
        value = payload.get(key)
        if value is None:
            raise ValueError(f"[{self.name}] missing required HMPASCII configuration key '{key}'.")
        text = str(value).strip()
        if not text:
            raise ValueError(f"[{self.name}] empty HMPASCII configuration key '{key}'.")
        return text

    def _require_int(self, payload: dict[str, Any], key: str) -> int:
        value = payload.get(key)
        if value is None or value == "":
            raise ValueError(f"[{self.name}] missing required HMPASCII configuration key '{key}'.")
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"[{self.name}] invalid integer value for HMPASCII configuration key '{key}': {value!r}"
            ) from exc

    def _require_sensor_id(self, params: dict[str, Any]) -> int:
        value = params.get("id")
        if value is None or value == "":
            raise ValueError(
                f"[{self.name}] missing required HMPASCII sensor id. "
                f"Set instruments.{self.name}.id in the YAML config."
            )
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"[{self.name}] invalid HMPASCII sensor id: {value!r}") from exc

    def _resolve_command_sequence(self, init_cfg: dict[str, Any]) -> list[str]:
        configured = init_cfg.get("command_sequence")
        if isinstance(configured, list) and configured:
            return [str(item) for item in configured]
        return ["SEND {id}"]

    def _query_measurement(self) -> str:
        if self.io_kind == "serial":
            return self._query_measurement_serial()
        return self._query_measurement_socket()

    def _query_measurement_serial(self) -> str:
        ser = self._serial_by_port[self.serial_port]
        lock = self._serial_lock_by_port[self.serial_port]

        with lock:
            if not ser.is_open:
                ser.open()

            for command in self.command_sequence:
                rendered = self._render_command(command)
                response = self._serial_send_and_read(ser, rendered)
                if response:
                    self.logger.debug("[%s] %s -> %r", self.name, rendered, response)
                if self._extract_measurement(response):
                    return response
            return ""

    def _query_measurement_socket(self) -> str:
        lock = self._socket_lock_by_endpoint[self._socket_endpoint_key(self.host, self.socket_port)]

        with lock:
            with socket.create_connection((self.host, self.socket_port), timeout=self.timeout) as sock:
                sock.settimeout(self.idle_timeout)
                self._socket_flush(sock)

                for command in self.command_sequence:
                    rendered = self._render_command(command)
                    response = self._socket_send_and_read(sock, rendered)
                    if response:
                        self.logger.debug("[%s] %s -> %r", self.name, rendered, response)
                    if self._extract_measurement(response):
                        return response
                return ""

    def _serial_send_and_read(self, ser: serial.Serial, command: str) -> str:
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        ser.write((command + "\r").encode("ascii", errors="ignore"))
        ser.flush()
        time.sleep(self.io_sleep)

        end = time.time() + self.timeout
        idle_deadline = time.time() + self.idle_timeout
        chunks = bytearray()

        while time.time() < end:
            waiting = ser.in_waiting
            if waiting:
                chunks.extend(ser.read(waiting))
                idle_deadline = time.time() + self.idle_timeout
                continue
            if time.time() >= idle_deadline:
                break
            time.sleep(0.02)

        return chunks.decode("latin-1", errors="replace").strip()

    def _socket_send_and_read(self, sock: socket.socket, command: str) -> str:
        self._socket_flush(sock)
        sock.sendall((command + "\r").encode("ascii", errors="ignore"))
        data = self._socket_read_until_idle(sock, total_timeout=self.timeout, idle_timeout=self.idle_timeout)
        time.sleep(self.io_sleep)
        return data.decode("latin-1", errors="replace").strip()

    def _socket_flush(self, sock: socket.socket) -> None:
        self._socket_read_until_idle(
            sock,
            total_timeout=min(0.5, self.timeout),
            idle_timeout=min(0.1, self.idle_timeout),
        )

    @staticmethod
    def _socket_read_until_idle(
        sock: socket.socket,
        *,
        total_timeout: float,
        idle_timeout: float,
    ) -> bytes:
        buf = bytearray()
        end = time.time() + total_timeout
        sock.settimeout(idle_timeout)
        while time.time() < end:
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)
            except socket.timeout:
                break
        return bytes(buf)

    def _extract_measurement(self, text: str) -> dict[str, float] | None:
        if not text:
            return None
        match = _MEAS_RE.search(text)
        if not match:
            return None

        out: dict[str, float] = {
            "t": float(match.group("t")),
            "rh": float(match.group("rh")),
        }
        td = match.group("td")
        if td is not None:
            out["td"] = float(td)
        return out

    def _render_command(self, template: str) -> str:
        return template.format(id=self.sensor_id).strip()

    def _note_failure(self, reason: str) -> None:
        self.fail_count += 1
        if self.fail_count >= self.max_fail_before_cooldown:
            self.cooldown_until = time.time() + self.cooldown_seconds
            self.logger.error(
                "[%s] repeated HMPASCII communication failures; backing off for %.0fs. Last reason: %s",
                self.name,
                self.cooldown_seconds,
                reason,
            )
        else:
            self.logger.warning(
                "[%s] HMPASCII communication failed (%s); fail_count=%s/%s",
                self.name,
                reason,
                self.fail_count,
                self.max_fail_before_cooldown,
            )

    def _ensure_shared_serial(self, port: str, io_cfg: dict[str, Any]) -> None:
        if port in self._serial_by_port:
            return

        self._serial_lock_by_port[port] = threading.Lock()
        self._serial_by_port[port] = serial.Serial(
            port=port,
            baudrate=int(io_cfg.get("baudrate", 19200)),
            bytesize=int(io_cfg.get("bytesize", 8)),
            parity=str(io_cfg.get("parity", "N")),
            stopbits=float(io_cfg.get("stopbits", 1)),
            timeout=float(io_cfg.get("timeout", 1.0)),
            write_timeout=float(io_cfg.get("write_timeout", 2.0)),
        )

    @staticmethod
    def _socket_endpoint_key(host: str, port: int) -> str:
        return f"{host}:{port}"

    @staticmethod
    def _now_string() -> str:
        return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
