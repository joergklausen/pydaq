"""
Ecotech / ACOEM nephelometer drivers built on the shared Instrument base.

Design
------
- `NEPH(Instrument)` is a single, configurable driver that supports **two protocols**:
  - **ACOEM/NE300** (default)
  - **Aurora3000**
- Pick the protocol via YAML parameter `instruments.<name>.params.model`:
  - "NE300", "NE-300", "ACOEM" → ACOEM protocol
  - "Aurora3000", "Aurora", "A3000" → Aurora protocol

Back-compat wrappers
--------------------
- `NE300(NEPH)` → forces model to ACOEM
- `Aurora3000(NEPH)` → forces model to Aurora

Both protocols rely on `Instrument` for config, logging, buffering, file I/O,
staging, and transfers. Serial lifecycle is handled by `@with_serial`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Tuple
import time

import numpy as np

from .instrument import Instrument, with_serial


# -----------------------------
# Helpers
# -----------------------------

def _iso(ts: datetime) -> str:
    """Return ISO8601 string without microseconds."""
    return ts.isoformat(timespec="seconds")


@dataclass(slots=True)
class _Cmds:
    """Container for instrument command strings (override via YAML `params`)."""
    read: str
    ident: str | None = None
    status: str | None = None


# -----------------------------
# Protocol strategies
# -----------------------------
class _AuroraProtocol:
    """Aurora 3000 protocol: VI099 (values), ID0 (id), VI088 (status)."""

    COLUMNS: tuple[str, ...] = (
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
    )

    def __init__(self, params: dict) -> None:
        c = (params.get("cmds", {}) if isinstance(params, dict) else {})
        self._cmds = _Cmds(
            read=str(c.get("read", "VI099")),
            ident=str(c.get("ident", "ID0")) if c.get("ident", None) is not None else None,
            status=str(c.get("status", "VI088")) if c.get("status", None) is not None else None,
        )
        header = params.get("header") if isinstance(params, dict) else None
        self._header = str(header or ",".join(("dtm", *self.COLUMNS)))

    # Commands
    def cmd_read(self) -> str: return self._cmds.read
    def cmd_ident(self) -> str | None: return self._cmds.ident
    def cmd_status(self) -> str | None: return self._cmds.status
    def header(self) -> str: return self._header

    # Parsing
    def parse(self, reading: str) -> Tuple[datetime, np.ndarray]:
        parts = [p.strip() for p in reading.split(",") if p.strip()]
        ts = datetime.strptime(parts[0], "%Y-%m-%d %H:%M:%S")
        floats = [float(x) for x in parts[1:-1]] if len(parts) > 2 else []
        status = int(parts[-1], 16) if len(parts) >= 2 else 0
        vec = np.array([*floats, float(status)], dtype=float)
        return ts, vec


class _AcoemProtocol:
    """ACOEM / NE-300 protocol (larger command set). Defaults are placeholders.

    Expected CSV-like payload with timestamp first (ISO or "YYYY-mm-dd HH:MM:SS")
    followed by numeric values. Any non-numeric token after the timestamp is ignored,
    except a final hex-like token (e.g., "0x1F" or "1F") which is converted to an int.
    """

    DEFAULT_HEADER = "dtm,fs_b,fs_g,fs_r,bs_b,bs_g,bs_r,T,RH,P,state"

    def __init__(self, params: dict) -> None:
        c = (params.get("cmds", {}) if isinstance(params, dict) else {})
        self._cmds = _Cmds(
            read=str(c.get("read", "***D")),  # live data dump (example)
            ident=str(c.get("ident", "ID")) if c.get("ident", None) is not None else None,
            status=str(c.get("status", "STAT")) if c.get("status", None) is not None else None,
        )
        header = params.get("header") if isinstance(params, dict) else None
        self._header = str(header or self.DEFAULT_HEADER)

    def cmd_read(self) -> str: return self._cmds.read
    def cmd_ident(self) -> str | None: return self._cmds.ident
    def cmd_status(self) -> str | None: return self._cmds.status
    def header(self) -> str: return self._header

    def parse(self, reading: str) -> Tuple[datetime, np.ndarray]:
        parts = [p.strip() for p in reading.split(",") if p.strip()]
        # timestamp
        p0 = parts[0]
        ts = datetime.fromisoformat(p0) if "T" in p0 else datetime.strptime(p0, "%Y-%m-%d %H:%M:%S")
        nums: list[float] = []
        # collect floats; convert trailing hex-like token to int if present
        for i, tok in enumerate(parts[1:], start=1):
            if i == len(parts) - 1 and any(ch in tok for ch in "ABCDEFabcdefx"):
                try:
                    nums.append(float(int(tok, 16)))
                    continue
                except Exception:
                    pass
            try:
                nums.append(float(tok))
            except ValueError:
                # ignore non-numeric tokens
                continue
        return ts, np.asarray(nums, dtype=float)


# =============================
# Unified driver
# =============================
class NEPH(Instrument):
    """Unified nephelometer driver with selectable protocol.

    Usage (YAML)
    ------------
    instruments:
      my_neph:
        driver: ecotech.NEPH
        params:
          model: NE300            # or Aurora3000
          cmds:                   # optional overrides
            read: "***D"
            ident: "ID"
            status: "STAT"
          header: "dtm,..."       # optional; falls back to protocol default
    """

    def __init__(self, config_path: str, name: str = "NEPH") -> None:
        super().__init__(name=name, config_path=config_path)

        # choose protocol based on params.model (default: ACOEM)
        params = self._params.get("params", {}) if isinstance(self._params, dict) else {}
        model = str(params.get("model", "NE300")).strip().lower()
        if model in {"aurora3000", "aurora", "a3000"}:
            self._proto = _AuroraProtocol(params)
        else:
            self._proto = _AcoemProtocol(params)

        # CSV header/extension
        self._filename_extension = "csv"
        self._header = self._proto.header()

    # ---------- protocol I/O ----------
    @with_serial
    def _serial_comm(self, cmd: str) -> str:  # type: ignore[override]
        """Send a command and return the ASCII response (CRLF normalised)."""
        assert self._serial is not None
        ser = self._serial
        try:
            try:
                ser.reset_input_buffer()
                ser.reset_output_buffer()
            except Exception:
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
        raise NotImplementedError("NEPH: socket mode not implemented")

    # ---------- high-level ops ----------
    def get_instrument_id(self) -> str:
        cmd = self._proto.cmd_ident()
        return self._serial_comm(cmd) if cmd else ""

    def get_status_word(self) -> str:
        cmd = self._proto.cmd_status()
        return self._serial_comm(cmd) if cmd else ""

    def get_current_data(self) -> str:
        return self._serial_comm(self._proto.cmd_read())

    # ---------- parsing & accumulation ----------
    def parse_current_data(self, reading: str) -> Tuple[datetime, np.ndarray]:
        return self._proto.parse(reading)

    def accumulate_data(self, data: str) -> None:  # type: ignore[override]
        with self._buf_lock:
            if data and not data.endswith("\n"):
                data += "\n"
            self._data += data

    def get_data(self) -> str:  # type: ignore[override]
        raw = self.get_current_data()
        ts, vec = self.parse_current_data(raw)
        if vec.size:
            # if last element represents a status word we keep it as int
            body_elems: list[str] = [*(f"{v:.3f}" for v in vec[:-1]), str(int(vec[-1]))] if vec.size > 1 else [str(int(vec[-1]))]
        else:
            body_elems = []
        line = ",".join(( _iso(ts), *body_elems ))
        self.accumulate_data(line)
        return line

    # ---------- config/time ops ----------
    def set_datetime(self) -> None:  # type: ignore[override]
        self.logger.info("NEPH.set_datetime(): not implemented (firmware-specific)")

    def get_config(self) -> dict:  # type: ignore[override]
        info: dict[str, str] = {}
        ident = self.get_instrument_id()
        status = self.get_status_word()
        if ident:
            info["id"] = ident
        if status:
            info["status"] = status
        return info

    def set_config(self) -> dict:  # type: ignore[override]
        return {}


# -----------------------------
# Back-compat wrappers
# -----------------------------
class NE300(NEPH):
    def __init__(self, config_path: str, name: str = "NE300") -> None:
        super().__init__(config_path=config_path, name=name)
        # force header/commands to ACOEM defaults unless explicitly overridden
        # (no-op here because NEPH already defaults to ACOEM)


class Aurora3000(NEPH):
    def __init__(self, config_path: str, name: str = "Aurora3000") -> None:
        super().__init__(config_path=config_path, name=name)
        # If user didn't set model, treat as Aurora; re-init header if needed
        params = self._params.get("params", {}) if isinstance(self._params, dict) else {}
        model = str(params.get("model", "")).strip().lower()
        if model not in {"aurora3000", "aurora", "a3000"}:
            # Rebind to Aurora protocol with current params
            self._proto = _AuroraProtocol(params)
            self._header = self._proto.header()


__all__ = ["NEPH", "NE300", "Aurora3000"]
