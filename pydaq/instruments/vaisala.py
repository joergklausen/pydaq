from __future__ import annotations

import logging
import re
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import serial

from pydaq.instruments.instrument import Instrument


_MEAS_RE = re.compile(
    r"T=\s*(?P<t>[+-]?\d+(?:\.\d+)?)\s*'C"
    r".*?RH=\s*(?P<rh>[+-]?\d+(?:\.\d+)?)\s*%RH"
    r"(?:.*?Td=\s*(?P<td>[+-]?\d+(?:\.\d+)?)\s*'C)?",
    re.IGNORECASE | re.DOTALL,
)


class HMPASCII(Instrument):
    """pydaq driver for Vaisala HMP ASCII sensors.

    This driver supports both:

    - serial-to-USB / direct serial connections
    - serial-to-TCP bridges such as the PLANET ICS-120 in raw TCP server mode

    Expected pydaq-style config examples are shown below.

    Serial example::

        instruments:
          hmp110_inlet:
            enabled: true
            driver: HMPASCII
            id: 1
            serial_number: U2110079
            io:
              kind: serial
              port: /dev/ttyUSB1
              baudrate: 19200
              bytesize: 8
              parity: N
              stopbits: 1
              timeout: 1.0
              write_timeout: 2.0
              sleep: 0.1
            init:
              command_sequence:
                - "SEND {id}"

    Socket example::

        instruments:
          hmp60:
            enabled: true
            driver: HMPASCII
            id: 5
            serial_number: T4744529-U0000072-R00A0A3B0
            io:
              kind: socket
              host: 192.168.0.100
              port: 5004
              timeout: 5.0
              idle_timeout: 0.25
              sleep: 0.1
            init:
              command_sequence:
                - "SEND"
                - "SEND {id}"
                - "OPEN {id}"
                - "SEND"
                - "CLOSE"

    Notes:
    - For shared RS-485 serial buses, one lock and one serial handle are reused per port.
    - For TCP bridges, one lock is reused per host/port so overlapping commands do not interleave.
    - The driver returns one record per poll with keys dtm, t, rh, td.
    """

    HEADERS = ["dtm", "t", "rh", "td"]

    _serial_by_port: dict[str, serial.Serial] = {}
    _serial_lock_by_port: dict[str, threading.Lock] = {}
    _socket_lock_by_endpoint: dict[str, threading.Lock] = {}

    def initialize(self) -> None:
        """Initialize transport settings and connection metadata."""
        inst_cfg = self._instrument_cfg()
        io_cfg = self._io_cfg()
        init_cfg = self._init_cfg()

        self.sensor_id = inst_cfg.get("id", init_cfg.get("address", 0))
        self.serial_number = inst_cfg.get("serial_number")
        self.model = (
            inst_cfg.get("model")
            or inst_cfg.get("type")
            or init_cfg.get("model")
            or "Vaisala HMP"
        )

        self.io_kind = str(io_cfg.get("kind", "serial")).lower()
        self.io_sleep = float(io_cfg.get("sleep", 0.1))
        self.timeout = float(io_cfg.get("timeout", 2.0))
        self.idle_timeout = float(io_cfg.get("idle_timeout", min(0.25, self.timeout)))
        self.wakeup_count = int(init_cfg.get("wakeup_count", 3))
        self.wakeup_delay = float(init_cfg.get("wakeup_delay", 0.05))
        self.command_sequence = self._resolve_command_sequence()

        self.max_fail_before_cooldown = int(init_cfg.get("max_fail_before_cooldown", 5))
        self.cooldown_seconds = float(init_cfg.get("cooldown_seconds", 120.0))
        self.fail_count = 0
        self.cooldown_until = 0.0

        if self.io_kind == "serial":
            self.port = str(io_cfg["port"])
            self._ensure_shared_serial(self.port, io_cfg)
            self.logger.info(
                "[%s] initialized Vaisala over serial port=%s id=%s",
                self.name,
                self.port,
                self.sensor_id,
            )
        elif self.io_kind in {"socket", "tcp"}:
            self.host = str(io_cfg["host"])
            self.port = int(io_cfg["port"])
            endpoint = self._socket_endpoint_key(self.host, self.port)
            if endpoint not in self._socket_lock_by_endpoint:
                self._socket_lock_by_endpoint[endpoint] = threading.Lock()
            self.logger.info(
                "[%s] initialized Vaisala over socket %s:%s id=%s",
                self.name,
                self.host,
                self.port,
                self.sensor_id,
            )
        else:
            raise ValueError(f"[{self.name}] unsupported io.kind={self.io_kind!r}")

    def collect_record(self) -> Dict[str, Any]:
        """Collect one Vaisala measurement record.

        Returns:
            A dictionary with keys dtm, t, rh, td. An empty dict is returned when
            communication fails or the reply does not contain a parseable reading.
        """
        if self.cooldown_until > time.time():
            return {}

        try:
            raw = self._query_measurement()
            parsed = self._extract_measurement(raw)
            if not parsed:
                self._note_failure("no parseable measurement in Vaisala reply")
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
        except Exception as err:
            self._note_failure(str(err))
            self.logger.error("[%s] collect_record failed: %s", self.name, err, exc_info=True)
            return {}

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------
    def _instrument_cfg(self) -> dict[str, Any]:
        """Return the per-instrument configuration with a few fallbacks."""
        for attr in ("instrument_config", "instrument_cfg"):
            value = getattr(self, attr, None)
            if isinstance(value, dict):
                return value

        root_cfg = self._root_cfg()
        if "io" in root_cfg or "schedule" in root_cfg or "output" in root_cfg:
            return root_cfg
        if isinstance(root_cfg.get("instruments"), dict) and self.name in root_cfg["instruments"]:
            return root_cfg["instruments"][self.name]
        if self.name in root_cfg and isinstance(root_cfg[self.name], dict):
            return root_cfg[self.name]
        return root_cfg

    def _root_cfg(self) -> dict[str, Any]:
        """Return the root configuration dict with compatible attribute names."""
        for attr in ("config", "cfg", "settings"):
            value = getattr(self, attr, None)
            if isinstance(value, dict):
                return value
        return {}

    def _io_cfg(self) -> dict[str, Any]:
        """Return I/O settings, supporting both pydaq and legacy nrbdaq layouts."""
        inst_cfg = self._instrument_cfg()
        root_cfg = self._root_cfg()

        if isinstance(inst_cfg.get("io"), dict):
            return dict(inst_cfg["io"])

        if "socket" in inst_cfg and isinstance(inst_cfg["socket"], dict):
            cfg = dict(inst_cfg["socket"])
            cfg.setdefault("kind", "socket")
            return cfg

        port_name = inst_cfg.get("port") or inst_cfg.get("serial_port")
        if port_name:
            cfg = {"kind": "serial", "port": port_name}
            if port_name in root_cfg and isinstance(root_cfg[port_name], dict):
                cfg.update(root_cfg[port_name])
            return cfg

        return {}

    def _init_cfg(self) -> dict[str, Any]:
        """Return initialization settings."""
        inst_cfg = self._instrument_cfg()
        init_cfg = inst_cfg.get("init")
        return dict(init_cfg) if isinstance(init_cfg, dict) else {}

    def _resolve_command_sequence(self) -> list[str]:
        """Resolve the sequence of commands used to fetch one reading."""
        init_cfg = self._init_cfg()
        configured = init_cfg.get("command_sequence")
        if isinstance(configured, list) and configured:
            return [str(item) for item in configured]

        if self.io_kind in {"socket", "tcp"}:
            return ["SEND", "SEND {id}", "OPEN {id}", "SEND", "CLOSE"]

        return ["SEND {id}"]

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------
    def _query_measurement(self) -> str:
        """Try the configured command sequence until a measurement is found."""
        if self.io_kind == "serial":
            return self._query_measurement_serial()
        return self._query_measurement_socket()

    def _query_measurement_serial(self) -> str:
        """Query the sensor through a shared serial connection."""
        ser = self._serial_by_port[self.port]
        lock = self._serial_lock_by_port[self.port]

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
        """Query the sensor through a raw TCP serial bridge."""
        lock = self._socket_lock_by_endpoint[self._socket_endpoint_key(self.host, self.port)]

        with lock:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
                sock.settimeout(self.idle_timeout)

                for _ in range(self.wakeup_count):
                    sock.sendall(b"\r")
                    time.sleep(self.wakeup_delay)
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
        """Send one command to a serial-connected sensor and read until idle."""
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
        """Send one command to a socket-connected sensor and read until idle."""
        self._socket_flush(sock)
        sock.sendall((command + "\r").encode("ascii", errors="ignore"))
        data = self._socket_read_until_idle(sock, total_timeout=self.timeout, idle_timeout=self.idle_timeout)
        time.sleep(self.io_sleep)
        return data.decode("latin-1", errors="replace").strip()

    def _socket_flush(self, sock: socket.socket) -> None:
        """Drain any pending bytes from the socket."""
        _ = self._socket_read_until_idle(
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
        """Read until no new bytes arrive for idle_timeout or total_timeout expires."""
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

    # ------------------------------------------------------------------
    # Parsing and state helpers
    # ------------------------------------------------------------------
    def _extract_measurement(self, text: str) -> Optional[Dict[str, float]]:
        """Extract T, RH and optional Td from a Vaisala ASCII response."""
        if not text:
            return None
        match = _MEAS_RE.search(text)
        if not match:
            return None

        out: Dict[str, float] = {
            "t": float(match.group("t")),
            "rh": float(match.group("rh")),
        }
        if match.group("td") is not None:
            out["td"] = float(match.group("td"))
        return out

    def _render_command(self, template: str) -> str:
        """Render a command template using the configured sensor id."""
        return template.format(id=self.sensor_id).strip()

    def _note_failure(self, reason: str) -> None:
        """Track repeated failures and activate a cooldown after many failures."""
        self.fail_count += 1
        if self.fail_count >= self.max_fail_before_cooldown:
            self.cooldown_until = time.time() + self.cooldown_seconds
            self.logger.error(
                "[%s] repeated Vaisala communication failures; backing off for %.0fs. Last reason: %s",
                self.name,
                self.cooldown_seconds,
                reason,
            )
        else:
            self.logger.warning(
                "[%s] Vaisala communication failed (%s); fail_count=%s/%s",
                self.name,
                reason,
                self.fail_count,
                self.max_fail_before_cooldown,
            )

    def _ensure_shared_serial(self, port: str, io_cfg: dict[str, Any]) -> None:
        """Create or reuse a shared serial object and lock for one OS serial port."""
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
        """Return a stable key for socket lock reuse."""
        return f"{host}:{port}"

    @staticmethod
    def _now_string() -> str:
        """Return the current timestamp in pydaq-friendly UTC text form."""
        return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


# Compatibility aliases if the registry or older imports expect these names.
HMP60ASCII = HMPASCII
HMP110ASCII = HMPASCII
