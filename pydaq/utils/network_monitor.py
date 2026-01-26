from __future__ import annotations

"""
network_monitor.py

A small, dependency-free reachability monitor intended to be integrated into the
`pydaq.py` orchestrator (or any similar scheduler-driven acquisition platform).

Why this exists
---------------
When a LAN-connected instrument stops responding, it is often unclear whether:

1) the instrument is off / unplugged / on a different VLAN / cable issue, OR
2) the driver / protocol implementation is broken.

This module provides a generic, periodic "is it reachable?" check that you can
run alongside data acquisition. It logs reachability *state changes* (UP/DOWN)
to help with troubleshooting and to create an audit trail in your log files.

Important caveats
-----------------
- ICMP ("ping") can be blocked by routers/firewalls/instrument settings. A "no ping"
  does *not* always mean the device is unreachable.
- For TCP-connected instruments, a TCP connect probe to the instrument's port is
  often the most meaningful check (it tests what you actually need: that a service
  is reachable).
- If an instrument accepts TCP connections but is not actually producing valid
  responses (application layer failure), this module may still report it as reachable.
  Use this as a *network/service reachability* indicator, not as a full protocol health check.

Integration pattern
-------------------
1) Build a list of ReachabilityTarget from your config (and/or from instrument objects).
2) Create NetworkMonitor(logger, targets, ...).
3) Schedule monitor.check_all() once per minute (or a cadence you choose).
4) Keep logs readable by logging on state changes only (default behavior).

Example (schedule library):
---------------------------
    targets = build_targets(cfg)
    monitor = NetworkMonitor(logger, targets, timeout_s=1.0)
    schedule.every(60).seconds.do(monitor.check_all)

    while True:
        schedule.run_pending()
        time.sleep(0.5)
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import platform
import socket
import subprocess
from typing import Optional


@dataclass(frozen=True)
class ReachabilityTarget:
    """
    One thing to monitor for reachability.

    Attributes
    ----------
    name:
        Human-friendly identifier used in log messages. Typically the instrument name
        (e.g., "ne300", "fidas") or infrastructure component (e.g., "router").

    host:
        Hostname or IP address (e.g., "192.168.0.200").

    port:
        TCP port to probe (if applicable). If set and method="auto", the probe will
        prefer TCP over ICMP. If method="tcp", port must be set.

    method:
        Probe method selector:
        - "auto": prefer TCP when port is known, otherwise ICMP ping
        - "tcp" : attempt a TCP connect to host:port
        - "icmp": use system ping
    """

    name: str
    host: str
    port: Optional[int] = None
    method: str = "auto"  # "auto" | "icmp" | "tcp"


@dataclass(frozen=True)
class ReachabilityResult:
    """
    Result of a reachability probe.

    Attributes
    ----------
    ok:
        True if reachable (probe succeeded), else False.

    method:
        The method that produced this result ("tcp" or "icmp").

    error:
        Optional diagnostic message on failure. Intended to be short and log-friendly.
    """

    ok: bool
    method: str
    error: Optional[str] = None


def _probe_tcp(host: str, port: int, timeout_s: float) -> ReachabilityResult:
    """
    Probe reachability by attempting a TCP connection.

    This checks that something is listening/accepting on the target port. It does not
    validate protocol-level health (no application data exchanged).

    Parameters
    ----------
    host:
        IP or hostname.
    port:
        TCP port to connect to.
    timeout_s:
        Socket timeout in seconds.

    Returns
    -------
    ReachabilityResult
        ok=True if connect succeeds, otherwise ok=False with an error string.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return ReachabilityResult(ok=True, method="tcp")
    except Exception as e:
        return ReachabilityResult(ok=False, method="tcp", error=str(e))


def _probe_icmp(host: str, timeout_s: float) -> ReachabilityResult:
    """
    Probe reachability using the system 'ping' command.

    This avoids raw sockets (and the privileges they may require). It relies on the OS
    ping utility being available. Because ping flags differ by OS, this function chooses
    conservative options for Windows vs. Unix-like systems.

    Parameters
    ----------
    host:
        IP or hostname.
    timeout_s:
        Timeout for ping. Note that OS ping implementations interpret timeouts differently.

    Returns
    -------
    ReachabilityResult
        ok=True if ping succeeds, otherwise ok=False and includes a diagnostic message.

    Notes
    -----
    Some networks/devices block ICMP echo requests. In such cases, ok=False does not
    necessarily mean "offline". Consider using TCP probes for LAN instruments.
    """
    system = platform.system().lower()

    if system.startswith("win"):
        # Windows:
        #  -n 1 : send 1 echo request
        #  -w   : timeout in milliseconds
        timeout_ms = max(1, int(timeout_s * 1000))
        cmd = ["ping", "-n", "1", "-w", str(timeout_ms), host]
    else:
        # Linux/macOS (common):
        #  -c 1 : send 1 echo request
        #  -W   : per-packet timeout in seconds (Linux). Some variants differ but usually work.
        cmd = ["ping", "-c", "1", "-W", str(max(1, int(timeout_s))), host]

    try:
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode == 0:
            return ReachabilityResult(ok=True, method="icmp")

        # Keep a short diagnostic snippet for logs.
        err = (p.stderr or p.stdout or "").strip()
        if len(err) > 300:
            err = err[-300:]
        return ReachabilityResult(ok=False, method="icmp", error=err or f"ping rc={p.returncode}")

    except FileNotFoundError:
        return ReachabilityResult(ok=False, method="icmp", error="ping not found")
    except Exception as e:
        return ReachabilityResult(ok=False, method="icmp", error=str(e))


def probe(target: ReachabilityTarget, timeout_s: float = 1.0) -> ReachabilityResult:
    """
    Probe a single target using the configured method.

    Parameters
    ----------
    target:
        The target to probe.
    timeout_s:
        Timeout used by the probe method.

    Returns
    -------
    ReachabilityResult
        The reachability status.

    Behavior
    --------
    - method="tcp": requires target.port and performs TCP connect probe.
    - method="icmp": performs system ping.
    - method="auto":
        - if port is set: try TCP first (more meaningful for services),
          and return the TCP result.
        - if port is not set: use ICMP ping.

    Rationale
    ---------
    For instruments, "is the TCP service reachable" is typically the most helpful signal.
    ICMP is kept as a fallback/diagnostic option.
    """
    method = target.method.lower()

    if method == "tcp":
        if target.port is None:
            return ReachabilityResult(ok=False, method="tcp", error="no port configured")
        return _probe_tcp(target.host, target.port, timeout_s)

    if method == "icmp":
        return _probe_icmp(target.host, timeout_s)

    # auto
    if target.port is not None:
        # Prefer TCP when a port is known.
        return _probe_tcp(target.host, target.port, timeout_s)

    return _probe_icmp(target.host, timeout_s)


class NetworkMonitor:
    """
    Periodic reachability monitor for multiple targets.

    This class is designed to be called periodically (e.g., once per minute) from a scheduler.
    By default, it logs only on *state changes* to avoid flooding log files.

    Logging policy
    --------------
    - First observation: logs one line per target (INFO if UP, WARNING if DOWN).
    - State changes:
        - DOWN -> UP logged as INFO
        - UP   -> DOWN logged as WARNING (+ error diagnostics if available)
    - Unchanged states: can optionally be logged every N ticks using log_unchanged_every_n.

    Parameters
    ----------
    logger:
        A standard Python logger (or loguru-like object) supporting .info/.warning/.debug.

    targets:
        List of ReachabilityTarget to monitor.

    timeout_s:
        Probe timeout in seconds.

    log_unchanged_every_n:
        If 0 (default), no "still up/down" logs are emitted.
        If > 0, an unchanged state will be logged every N calls to check_all().
        Useful if you want periodic breadcrumbs when something stays DOWN.

    Typical use in pydaq.py
    -----------------------
    - Build targets from config:
        - include all instruments with socket.host (+ port if present)
        - optionally include infrastructure (router/modem/switch)
    - Schedule:
        - schedule.every(60).seconds.do(monitor.check_all)
    """

    def __init__(
        self,
        logger,
        targets: list[ReachabilityTarget],
        timeout_s: float = 1.0,
        log_unchanged_every_n: int = 0,
    ):
        self.logger = logger
        self.targets = targets
        self.timeout_s = timeout_s
        self.log_unchanged_every_n = log_unchanged_every_n

        # key -> last ok status
        self._state: dict[str, bool] = {}
        # key -> ticks since last log-worthy event (state change or periodic log)
        self._ticks: dict[str, int] = {}


    def set_targets(self, targets: list[ReachabilityTarget], prune_state: bool = True) -> None:
        """
        Replace the current target list (typically after a config hot-reload).

        Parameters
        ----------
        targets:
            New list of targets to monitor.

        prune_state:
            If True (default), drop remembered state/tick counters for targets that are
            no longer present. This prevents unbounded growth if targets come/go over time.

        Notes
        -----
        This does *not* emit any log messages by itself. The next call to ``check_all()``
        will treat newly-added targets as "first observation" and log once for each of them.
        """
        self.targets = targets

        if not prune_state:
            return

        keep = {self._key(t) for t in targets}
        self._state = {k: v for k, v in self._state.items() if k in keep}
        self._ticks = {k: v for k, v in self._ticks.items() if k in keep}

    @staticmethod
    def _key(t: ReachabilityTarget) -> str:
        """
        Create a stable, log-friendly key for state tracking.

        Format:
            "<name>@<host>" or "<name>@<host>:<port>"
        """
        return f"{t.name}@{t.host}{'' if t.port is None else ':' + str(t.port)}"

    def check_all(self) -> None:
        """
        Probe all targets once and emit logs based on the logging policy.

        This method is intended to be scheduled periodically. It is synchronous and short-lived.
        Keep timeout_s small (1–2 s) to avoid stalling your acquisition loop if many targets are down.

        Notes
        -----
        If you monitor many targets and want strict worst-case bounds, you can:
        - reduce timeout_s
        - probe fewer targets per tick (round-robin)
        - or adapt this module to use threads/async (not included here by design).
        """
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        for t in self.targets:
            key = self._key(t)
            res = probe(t, timeout_s=self.timeout_s)

            prev = self._state.get(key)
            self._state[key] = res.ok
            self._ticks[key] = self._ticks.get(key, 0) + 1

            # First observation -> always log
            if prev is None:
                lvl = self.logger.info if res.ok else self.logger.warning
                lvl(
                    f"[net] {now} {key} reachable={res.ok} method={res.method}"
                    + (f" err={res.error}" if res.error else "")
                )
                self._ticks[key] = 0
                continue

            # State change -> always log
            if res.ok != prev:
                if res.ok:
                    self.logger.info(f"[net] {now} {key} UP (method={res.method})")
                else:
                    self.logger.warning(
                        f"[net] {now} {key} DOWN (method={res.method})"
                        + (f" err={res.error}" if res.error else "")
                    )
                self._ticks[key] = 0
                continue

            # Unchanged -> optionally log every N ticks
            if self.log_unchanged_every_n and self._ticks[key] >= self.log_unchanged_every_n:
                lvl = self.logger.debug if res.ok else self.logger.warning
                lvl(
                    f"[net] {now} {key} still {'UP' if res.ok else 'DOWN'} (method={res.method})"
                    + (f" err={res.error}" if res.error else "")
                )
                self._ticks[key] = 0
