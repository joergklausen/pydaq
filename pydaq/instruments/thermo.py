"""pydaq.instruments.thermo

Thermo Scientific 49-series ozone instrument drivers.

Supported models
----------------
- Thermo49C: typically queried via ``lrec``.
- Thermo49i: commonly queried via ``lr00``.
- Thermo49CPS: 49C-PS calibrator; can optionally cycle through setpoints.

Output convention
-----------------
- Every record includes ``dtm`` (PC acquisition time), written by the platform.
- Instrument internal time/date are included as ``time`` and ``date`` (strings).

Communications
--------------
These instruments are typically addressed by a leading address byte, followed by an ASCII
command terminated by ``\\r``. The address byte is commonly ``id + 128`` (id in 0..127).
"""

from __future__ import annotations

from datetime import datetime
import re
import time
from typing import Any, Dict, Iterable, Optional

from pydaq.instruments.instrument import Instrument, LineComms, utc_timestamp_string


_FLOAT_RE = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


def _first_float(text: str) -> Optional[float]:
    """Extract the first float-like number from text."""
    m = _FLOAT_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(0))
    except Exception:
        return None


def _strip_echo_and_checksum(raw: str, cmd: str) -> str:
    """Remove command echo and optional '*checksum' suffix."""
    s = raw.strip()
    if "*" in s:
        s = s.split("*", 1)[0].strip()
    if s.startswith(cmd):
        s = s[len(cmd):].lstrip()
    else:
        idx = s.find(cmd)
        if idx != -1:
            s = (s[:idx] + s[idx + len(cmd):]).strip()
    return s.strip()


class Thermo49Base(Instrument):
    """Base class for Thermo 49-series drivers."""

    # Platform CSV headers (standardized, no pcdate/pctime).
    HEADERS = [
        "dtm",
        "time",
        "date",
        "o3",
        "flags",
        "hio3",
        "cellai",
        "cellbi",
        "bncht",
        "lmpt",
        "o3lt",
        "flowa",
        "flowb",
        "pres",
    ]

    DEFAULT_SAMPLE_COMMAND = "o3"

    # Token schema for record-style replies
    POSITIONAL_FIELDS: tuple[str, ...] = ("time", "date", "o3")

    _FIELD_COERCERS: Dict[str, Any] = {
        "o3": float,
        "hio3": float,
        "cellai": int,
        "cellbi": int,
        "bncht": float,
        "lmpt": float,
        "o3lt": float,
        "flowa": float,
        "flowb": float,
        "pres": float,
    }

    def initialize(self) -> None:
        """Initialize comms and apply optional one-time configuration."""
        self._ensure_comms()

        init_cfg = self._params_section("init")

        if bool(init_cfg.get("set_datetime", False)):
            self._set_instrument_datetime()

        for cmd in init_cfg.get("get_config", []) or []:
            cmd_str = str(cmd).strip()
            if cmd_str:
                resp = self._send(cmd_str)
                if resp:
                    self.logger.info("get_config cmd=%s resp=%s", cmd_str, resp)
                else:
                    self.state.last_error = f"no response to get_config cmd={cmd_str}"
                    self.logger.error("no response to get_config cmd=%s", cmd_str)

        for cmd in init_cfg.get("set_config", []) or []:
            cmd_str = str(cmd).strip()
            if cmd_str:
                resp = self._send(cmd_str)
                if resp:
                    self.logger.info("set_config cmd=%s resp=%s", cmd_str, resp)
                else:
                    self.state.last_error = f"no response to set_config cmd={cmd_str}"
                    self.logger.error("no response to set_config cmd=%s", cmd_str)

    def get_record(self) -> Dict[str, Any]:
        """Retrieve one record from the instrument.

        Returns:
            Record mapping including ``dtm`` and parsed fields. Returns an empty dict on failure.
        """
        self._ensure_comms()

        processing_cfg = self._params_section("processing")
        cmd = str(processing_cfg.get("sample_command", self.DEFAULT_SAMPLE_COMMAND)).strip() or self.DEFAULT_SAMPLE_COMMAND

        # PC acquisition timestamp (platform canonical)
        dtm = utc_timestamp_string()

        raw = self._send(cmd)
        if not raw:
            self.state.last_error = f"no response to sample command '{cmd}'"
            self.logger.error("no response to sample command cmd=%s", cmd)
            return {}

        payload = _strip_echo_and_checksum(raw, cmd)
        if not payload:
            self.state.last_error = f"empty payload after stripping echo/checksum for cmd '{cmd}'"
            self.logger.error("empty payload after stripping echo/checksum cmd=%s raw=%r", cmd, raw[:200])
            return {}

        record: Dict[str, Any] = {"dtm": dtm}

        # Simple "o3" query often returns a bare number.
        if cmd.lower() in {"o3", "o3?"}:
            value = _first_float(payload)
            if value is None:
                self.state.last_error = f"could not parse scalar response for cmd '{cmd}'"
                self.logger.error("could not parse scalar response cmd=%s payload=%r", cmd, payload[:200])
                return {}
            scale = float(processing_cfg.get("o3_scale", 1.0))
            record["o3"] = value * scale
            return record

        parsed = self._parse_fields(payload)
        if not parsed:
            self.state.last_error = f"could not parse record response for cmd '{cmd}'"
            self.logger.error("could not parse record response cmd=%s payload=%r", cmd, payload[:200])
            return {}

        record.update(parsed)

        # Ensure an o3 value exists if any float exists.
        if "o3" not in record:
            value = _first_float(payload)
            if value is not None:
                record["o3"] = value

        return record

    # --------------------------
    # Shared comms
    # --------------------------

    def _params_section(self, key: str) -> Dict[str, Any]:
        value = (self.parameters or {}).get(key, {})
        return value if isinstance(value, dict) else {}

    def _ensure_comms(self) -> None:
        if not hasattr(self, "_line"):
            io_cfg = self._params_section("io")
            self._line = LineComms(io_cfg, logger=self.logger)
        if not hasattr(self, "_instrument_id_byte"):
            self._instrument_id_byte = self._resolve_instrument_id_byte()

    def _resolve_instrument_id_byte(self) -> bytes:
        processing_cfg = self._params_section("processing")
        raw_id = processing_cfg.get("id", (self.parameters or {}).get("id"))

        if raw_id is None:
            raise ValueError(
                f"[{self.name}] Thermo instrument configuration requires "
                "'id' in the range 0..127"
            )

        try:
            device_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"[{self.name}] Invalid Thermo instrument id={raw_id!r}; "
                "expected an integer in the range 0..127"
            ) from exc

        if not 0 <= device_id <= 127:
            raise ValueError(
                f"[{self.name}] Invalid Thermo instrument id={device_id}; "
                "expected 0..127"
            )

        return bytes([device_id + 128])

    def _send(self, cmd: str) -> str:
        """Send an addressed command and return reply (best-effort)."""
        return self._line.request(cmd, prefix=self._instrument_id_byte)

    # --------------------------
    # Parsing helpers
    # --------------------------

    def _parse_fields(self, payload: str) -> Dict[str, Any]:
        tokens = payload.split()
        if not tokens:
            return {}

        lower_tokens = [t.lower() for t in tokens]
        known_labels = {
            "flags", "o3", "hio3", "cellai", "cellbi", "bncht", "lmpt", "o3lt", "flowa", "flowb", "pres"
        }

        if any(t in known_labels for t in lower_tokens):
            return self._parse_key_value(tokens)

        return self._parse_positional(tokens)

    def _parse_key_value(self, tokens: Iterable[str]) -> Dict[str, Any]:
        items = list(tokens)
        out: Dict[str, Any] = {}

        # Some formats start with instrument time/date (unlabeled)
        if len(items) >= 2 and re.match(r"^\d{1,2}:\d{2}", items[0]):
            out["time"] = items[0]
            out["date"] = items[1]
            items = items[2:]

        i = 0
        while i < len(items) - 1:
            key = items[i].lower()
            val = items[i + 1]
            if key in self._FIELD_COERCERS:
                out[key] = self._coerce_value(key, val)
                i += 2
                continue
            if key == "flags":
                out["flags"] = val
                i += 2
                continue
            i += 1

        return out

    def _parse_positional(self, tokens: list[str]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}

        # If the reply begins with time/date, map those first.
        for field, token in zip(self.POSITIONAL_FIELDS, tokens):
            if field in self._FIELD_COERCERS:
                out[field] = self._coerce_value(field, token)
            else:
                out[field] = token

        return out

    def _coerce_value(self, field: str, token: str) -> Any:
        coercer = self._FIELD_COERCERS.get(field)
        if coercer is None:
            return token
        try:
            return coercer(token)
        except Exception:
            if coercer is int:
                try:
                    return int(float(token))
                except Exception:
                    return None
            return None

    # --------------------------
    # Instrument configuration helpers
    # --------------------------

    def _set_instrument_datetime(self) -> None:
        """Set instrument date and time (best-effort)."""
        now = datetime.now()
        self._send(f"set date {now.strftime('%m-%d-%y')}")
        self._send(f"set time {now.strftime('%H:%M:%S')}")


class Thermo49C(Thermo49Base):
    """Thermo 49C ozone analyzer."""
    DEFAULT_SAMPLE_COMMAND = "lrec"
    POSITIONAL_FIELDS = (
        "time",
        "date",
        "o3",
        "flags",
        "cellai",
        "cellbi",
        "bncht",
        "lmpt",
        "o3lt",
        "flowa",
        "flowb",
        "pres",
    )


class Thermo49i(Thermo49Base):
    """Thermo 49i ozone analyzer."""
    DEFAULT_SAMPLE_COMMAND = "lr00"
    POSITIONAL_FIELDS = (
        "time",
        "date",
        "flags",
        "o3",
        "hio3",
        "cellai",
        "cellbi",
        "bncht",
        "lmpt",
        "o3lt",
        "flowa",
        "flowb",
        "pres",
    )


class Thermo49CPS(Thermo49C):
    """Thermo 49C-PS ozone calibrator.

    Adds a ``setpoint_ppb`` field and optionally cycles through configured setpoints.

    Example config::

        processing:
          levels_ppb: [0, 50, 100]
          level_hold_seconds: 600
          level_command_template: "set o3 conc {level}"
    """

    HEADERS = Thermo49Base.HEADERS + ["setpoint_ppb"]

    def initialize(self) -> None:
        super().initialize()

        proc = self._params_section("processing")
        self._levels_ppb = [float(x) for x in (proc.get("levels_ppb") or [])]
        self._level_hold_seconds = float(proc.get("level_hold_seconds", 0.0))
        self._level_template = str(proc.get("level_command_template", "set o3 conc {level}"))

        self._level_index = 0
        self._current_setpoint: Optional[float] = None
        self._last_level_change_utc = time.time()

        if self._levels_ppb:
            self._apply_setpoint(self._levels_ppb[0])

    def get_record(self) -> Dict[str, Any]:
        self._maybe_advance_level()
        record = super().get_record()
        if not record:
            return {}
        record["setpoint_ppb"] = self._current_setpoint
        return record

    def _maybe_advance_level(self) -> None:
        if not self._levels_ppb or self._level_hold_seconds <= 0:
            return
        now = time.time()
        if (now - self._last_level_change_utc) < self._level_hold_seconds:
            return
        self._level_index = (self._level_index + 1) % len(self._levels_ppb)
        self._apply_setpoint(self._levels_ppb[self._level_index])
        self._last_level_change_utc = now

    def _apply_setpoint(self, level_ppb: float) -> None:
        cmd = self._level_template.format(
            level=int(level_ppb) if float(level_ppb).is_integer() else level_ppb
        )
        resp = self._send(cmd)
        if resp:
            self.logger.info("setpoint level_ppb=%s resp=%s", level_ppb, resp)
        self._current_setpoint = float(level_ppb)
