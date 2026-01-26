from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Tuple

import numpy as np
import colorama

from ..instrument import Instrument


class AuroraClient:
    """
    Aurora 3000 protocol implementation (VI commands + `***R` / `***D`).

    The NEPH driver decides whether to talk over serial or TCP socket; this
    class only constructs commands and parses responses.
    """

    def __init__(self, driver: Instrument, params: Dict[str, Any]) -> None:
        self._drv = driver
        self.logger = driver.logger
        try:
            self.serial_id = int(getattr(driver, "_serial_id", params.get("serial_id", 1)))
        except Exception:
            self.serial_id = 1

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    def _send(self, cmd: str, expect_response: bool = True) -> str:
        """
        Send an ASCII command and return its response as text.
        """
        try:
            # Decide backend based on the driver's communication mode.
            use_serial = getattr(self._drv, "_params_comms", None) == "serial"
            if use_serial:
                text = self._drv._serial_comm(cmd)
            else:
                text = self._drv._socket_comm(cmd)
            return text.strip()
        except Exception as err:
            self.logger.error(
                f"{colorama.Fore.RED}Aurora command '{cmd}' failed: {err}{colorama.Fore.GREEN}"
            )
            if expect_response:
                raise
            return ""

    # ------------------------------------------------------------------
    # Public protocol API used by NEPH
    # ------------------------------------------------------------------

    def get_current_data(self, sep: str = ",") -> str:
        """
        Retrieve current data.

        For Aurora, this uses the ``VI099`` command (voltage input block),
        with station ID prefix when using a socket.
        """
        cmd = "VI099" if self._drv._use_serial() else f"VI{self.serial_id:02d}99"
        raw = self._send(cmd)
        # Normalise separators for pydaq
        return raw.replace(", ", ",").replace(",", sep)

    def get_status_word(self) -> str:
        cmd = "VI088" if self._drv._use_serial() else f"VI{self.serial_id:02d}88"
        return self._send(cmd)

    def parse_current_data(self, reading: str) -> Tuple[datetime, np.ndarray]:
        """
        Parse a comma-separated Aurora reading (as returned by ``VI099``).

        Layout (typical):

        - 0:  ISO datetime (YYYY-mm-dd HH:MM:SS)
        - 1..N-2: floats
        - N-1:    hex status word
        """
        parts = [p.strip() for p in reading.split(",") if p.strip()]
        if len(parts) < 2:
            raise ValueError(f"Cannot parse Aurora reading: {reading!r}")

        timestamp = datetime.strptime(parts[0], "%Y-%m-%d %H:%M:%S")
        floats = [float(x) for x in parts[1:-1]]
        status = int(parts[-1], 16)  # hex → int
        return timestamp, np.array([*floats, float(status)], dtype=float)

    def get_values(self, parameters: Iterable[int], verbosity: int = 0) -> Dict[int, Any]:
        """
        Minimal "values" support via VI commands.

        This is *not* a general Aurora parameter interface; it currently only
        supports a few special cases (e.g. operating state via VI71).
        """
        result: Dict[int, Any] = {}
        for p in parameters:
            if p == 71:
                cmd = f"VI{self.serial_id:02d}{p:02d}"
                resp = self._send(cmd)
                mapping = {"000": 0, "016": 2, "032": 1}
                result[p] = mapping.get(resp.strip(), 9)
        if verbosity > 0:
            self.logger.debug("Aurora get_values(%s) → %s", list(parameters), result)
        return result

    def set_value(
        self,
        parameter_id: int,
        value: int,
        verify: bool = True,
        verbosity: int = 0,
    ) -> int:
        """
        Setting arbitrary Aurora parameters via VI is not implemented.

        NEPH never calls this for Aurora at the moment.
        """
        raise NotImplementedError("Setting Aurora parameters via this client is not implemented.")

    def get_datetime(self, verbosity: int = 0) -> datetime:
        """
        Get instrument datetime via Aurora VI commands.

        Uses:

        - ``VI64``: format string (D/M/Y, M/D/Y, Y-M-D)
        - ``VI80``: date
        - ``VI81``: time
        """
        fmt = self._send(f"VI{self.serial_id:02d}64")
        dte = self._send(f"VI{self.serial_id:02d}80")
        tme = self._send(f"VI{self.serial_id:02d}81")
        return self._aurora_timestamp_to_datetime(fmt, dte, tme)

    def _aurora_timestamp_to_datetime(self, fmt: str, dte: str, tme: str) -> datetime:
        fmt_clean = fmt.strip().replace(" ", "").replace("\r\n", "")
        d_clean = dte.strip().replace("\r\n", "")
        t_clean = tme.strip().replace("\r\n", "")

        if fmt_clean == "D/M/Y":
            date_fmt = "%d/%m/%Y"
        elif fmt_clean == "M/D/Y":
            date_fmt = "%m/%d/%Y"
        elif fmt_clean == "Y-M-D":
            date_fmt = "%Y-%m-%d"
        else:
            date_fmt = "%Y-%m-%d"

        date_obj = datetime.strptime(d_clean, date_fmt).date()
        time_obj = datetime.strptime(t_clean, "%H:%M:%S").time()
        return datetime.combine(date_obj, time_obj)

    def set_datetime(self, dtm: datetime, verbosity: int = 0) -> None:
        """
        Setting Aurora datetime is instrument/firmware-specific and not implemented here.
        """
        raise NotImplementedError("Aurora set_datetime not implemented.")

    def get_id(self, verbosity: int = 0) -> Dict[str, str]:
        ident: Dict[str, str] = {"protocol": "aurora", "serial_id": str(self.serial_id)}
        try:
            dtm = self.get_datetime(verbosity=verbosity)
            ident["datetime"] = dtm.isoformat()
        except Exception:
            pass
        return ident

    def get_current_operation(self, verbosity: int = 0) -> int:
        """
        Retrieve operating state from VI71 (see original driver):

        - "000": 0 (Normal)
        - "032": 1 (Zero)
        - "016": 2 (Span)
        """
        resp = self._send(f"VI{self.serial_id:02d}71")
        mapping = {"000": 0, "016": 2, "032": 1}
        return mapping.get(resp.strip(), 9)

    def set_current_operation(
        self,
        state: int = 0,
        verify: bool = True,
        verbosity: int = 0,
    ) -> int:
        """
        Changing Aurora operation state is normally done via digital IO lines,
        not VI commands. This is left unimplemented.
        """
        raise NotImplementedError("Aurora set_current_operation is not implemented.")

    # ------------------------------------------------------------------
    # Logged-data retrieval (`***R` / `***D`)
    # ------------------------------------------------------------------

    def get_logged_data(
        self,
        start: datetime,
        end: datetime | None = None,
        verbosity: int = 0,
    ) -> List[Dict[str | int, Any]]:
        """
        Retrieve data from the Aurora internal logger using ``***R`` / ``***D``.

        Note: the start/end arguments are **ignored** – the instrument decides
        which period to return. If you need strict time selection, you should
        use the ACOEM binary logger instead (NE-300).
        """
        records: List[Dict[str | int, Any]] = []
        try:
            # Reset pointer to first entry
            self._send("***R", expect_response=False)
        except Exception as err:
            self.logger.error(
                f"{colorama.Fore.RED}Aurora ***R command failed: {err}{colorama.Fore.GREEN}"
            )
            return []

        # Retrieve until the instrument stops sending data.
        # A hard cap avoids infinite loops in case of protocol issues.
        max_records = 10_000
        for _ in range(max_records):
            try:
                line = self._send("***D")
            except Exception:
                break
            line = line.strip()
            if not line:
                break
            try:
                ts, vals = self.parse_current_data(line)
            except Exception:
                # Skip malformed lines
                continue
            rec: Dict[str | int, Any] = {"dtm": ts.strftime("%Y-%m-%d %H:%M:%S")}
            for idx, v in enumerate(vals, start=1):
                rec[idx] = float(v)
            records.append(rec)

        if verbosity > 0:
            self.logger.info("Aurora logged-data records retrieved: %d", len(records))
        return records

    def logged_data_to_csv(self, records: List[Dict[str | int, Any]], sep: str = ",") -> str:
        if not records:
            return ""

        first = dict(records[0])
        dtm_key = "dtm"
        if dtm_key in first:
            first.pop(dtm_key)
        keys = list(first.keys())

        header = sep.join([dtm_key] + [str(k) for k in keys])
        lines = [header]

        for rec in records:
            d = dict(rec)
            dtm_value = d.pop(dtm_key, "")
            row = [str(dtm_value)] + [str(d.get(k, "")) for k in keys]
            lines.append(sep.join(row))

        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------
    # Config snapshot hooks (for NEPH.get_config / set_config)
    # ------------------------------------------------------------------

    def get_config_snapshot(self) -> Dict[str, Any]:
        # Aurora currently doesn't expose a structured config snapshot here.
        return {}

    def set_config_snapshot(self) -> Dict[str, Any]:
        # Nothing to set programmatically for Aurora at this time.
        return {}
