#!/usr/bin/env python3
"""
find_49i_ip_address.py - minimal brute-force scanner to find a Thermo 49i by sending only "o3\\r".

Goal:
- Find IP(s) where TCP connect succeeds AND sending "o3" yields ANY response bytes.

Defaults:
- Scan 192.168.0.0/24
- Port 9880 (49i C-Link over Ethernet)
- Also tries C-Link instrument ID prefix styles for robustness:
    * ID=0  -> send b"o3\\r"
    * ID=49 -> send b"\\xB1o3\\r"  (49 + 128)

You can extend scanning to additional 192.168.N.0/24 networks with --extend-192.

Examples:
  python find_49i_ip_address.py
  python find_49i_ip_address.py --extend-192 8
  python find_49i_ip_address.py --networks 192.168.3.0/24,192.168.4.0/24
  python find_49i_ip_address.py --timeout 0.3 --workers 512
"""

from __future__ import annotations

import argparse
import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Iterable, Sequence


DEFAULT_PORT = 9880
DEFAULT_TIMEOUT = 0.5
DEFAULT_WORKERS = 256

# Only probe: "o3\r"
O3_CMD = b"o3\r"

# Try both common C-Link styles:
# - no prefix (instrument ID 0)
# - prefix byte (instrument ID 49 default -> 49+128 = 177 = 0xB1)
PROBES = [
    ("plain", O3_CMD),
    ("clink_id49", bytes([0xB1]) + O3_CMD),
]


@dataclass(frozen=True)
class Found:
    ip: str
    port: int
    probe: str
    nbytes: int


def _iter_hosts(net: ipaddress.IPv4Network) -> Iterable[ipaddress.IPv4Address]:
    yield from net.hosts()


def _parse_networks(s: str) -> list[ipaddress.IPv4Network]:
    nets: list[ipaddress.IPv4Network] = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        nets.append(ipaddress.ip_network(part, strict=False))  # type: ignore[arg-type]
    return nets


def _extend_192_168(n: int) -> list[ipaddress.IPv4Network]:
    n = max(0, min(256, n))
    return [ipaddress.ip_network(f"192.168.{i}.0/24") for i in range(n)]


def _dedup_networks(networks: list[ipaddress.IPv4Network]) -> list[ipaddress.IPv4Network]:
    uniq: dict[str, ipaddress.IPv4Network] = {}
    for n in networks:
        if isinstance(n, ipaddress.IPv6Network):
            continue
        uniq[str(n)] = n
    return list(uniq.values())


def _try_ip(ip: str, port: int, timeout: float) -> list[Found]:
    found: list[Found] = []
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        # For each probe style, send and require at least 1 byte back
        for name, payload in PROBES:
            try:
                s.sendall(payload)
            except OSError:
                continue
            try:
                data = s.recv(64)
            except socket.timeout:
                data = b""
            if data:
                found.append(Found(ip=ip, port=port, probe=name, nbytes=len(data)))
                # We only need the IP; stop after first successful response
                break
    except (socket.timeout, ConnectionRefusedError, OSError):
        pass
    finally:
        try:
            s.close()
        except Exception:
            pass
    return found


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description='Scan for 49i by requiring a response to "o3".')
    p.add_argument("--networks", default="192.168.0.0/24", help="Comma-separated CIDRs to scan.")
    p.add_argument("--extend-192", type=int, default=0, help="Also scan 192.168.0..(N-1).0/24.")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="TCP port to probe (default: 9880).")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Per-connection timeout seconds.")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="Thread pool size.")
    args = p.parse_args(argv)

    networks = _parse_networks(args.networks)
    if args.extend_192:
        networks.extend(_extend_192_168(args.extend_192))
    networks = _dedup_networks(networks)

    print(f"Scanning {len(networks)} network(s) on TCP {args.port} (timeout={args.timeout}s, workers={args.workers})")
    for n in networks:
        print(f"  - {n}")

    futures = []
    hits: dict[str, Found] = {}  # dedup by IP

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for net in networks:
            for host in _iter_hosts(net):
                futures.append(ex.submit(_try_ip, str(host), args.port, args.timeout))

        for fut in as_completed(futures):
            res = fut.result()
            for f in res:
                if f.ip not in hits:
                    hits[f.ip] = f
                    print(f"FOUND {f.ip}:{f.port} (probe={f.probe}, bytes={f.nbytes})")

    print(f"\nDone. Found {len(hits)} IP(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())