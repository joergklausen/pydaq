"""Read-only SNMPv3 inventory of an Eaton Network-M2 UPS card.

Install: python -m pip install 'pysnmp>=7.1,<8'
Example: python test_eaton_snmp.py --host 192.168.1.141 --user admin --level noAuthNoPriv

Passwords are requested interactively and never written to the output file.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path

from pysnmp.hlapi.v3arch.asyncio import (
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    UsmUserData,
    get_cmd,
    usmAesCfb128Protocol,
    usmHMACSHAAuthProtocol,
    walk_cmd,
)

TREES = (
    ("System identification", "1.3.6.1.2.1.1", 30),
    ("Standard UPS MIB (RFC 1628)", "1.3.6.1.2.1.33", 350),
    ("Eaton enterprise MIB", "1.3.6.1.4.1.534", 350),
)


def credentials(args: argparse.Namespace) -> UsmUserData:
    if args.level == "noAuthNoPriv":
        return UsmUserData(args.user)
    auth_key = getpass("SNMPv3 authentication password: ")
    if args.level == "authNoPriv":
        return UsmUserData(args.user, authKey=auth_key,
                           authProtocol=usmHMACSHAAuthProtocol)
    priv_key = getpass("SNMPv3 privacy password: ")
    return UsmUserData(args.user, authKey=auth_key, privKey=priv_key,
                       authProtocol=usmHMACSHAAuthProtocol,
                       privProtocol=usmAesCfb128Protocol)


async def collect(args: argparse.Namespace) -> list[str]:
    lines = [f"Eaton SNMP inventory | {datetime.now(timezone.utc).isoformat()}",
             f"Host: {args.host} | Security: {args.level}"]
    user = credentials(args)
    engine = SnmpEngine()
    target = await UdpTransportTarget.create((args.host, args.port), timeout=3, retries=1)
    try:
        # A single GET first: distinguishes connection failure from an empty MIB subtree.
        error, status, index, binds = await get_cmd(
            engine, user, target, ContextData(),
            ObjectType(ObjectIdentity("1.3.6.1.2.1.1.1.0")),
            lookupMib=False,
        )
        if error or status:
            raise RuntimeError(f"SNMP GET failed: {error or status}")
        lines.append("SNMP GET succeeded: " + "; ".join(
            f"{oid.prettyPrint()} = {value.prettyPrint()}" for oid, value in binds))
        for name, root, limit in TREES:
            lines.append(f"\n--- {name} ({root}) ---")
            n = 0
            async for error, status, index, binds in walk_cmd(
                engine, user, target, ContextData(),
                ObjectType(ObjectIdentity(root)),
                lexicographicMode=False, lookupMib=False,
                maxRows=limit, maxCalls=limit,
            ):
                if error or status:
                    lines.append(f"ERROR: {error or status}")
                    break
                for oid, value in binds:
                    oid_text = oid.prettyPrint()
                    if not (oid_text == root or oid_text.startswith(root + ".")):
                        continue
                    lines.append(f"{oid_text} = {value.prettyPrint()}")
                    n += 1
            lines.append(f"Objects retrieved: {n}" + (" (limit reached)" if n >= limit else ""))
    finally:
        engine.close_dispatcher()
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="UPS card IP address or DNS name")
    parser.add_argument("--user", required=True, help="SNMPv3 username")
    parser.add_argument("--port", type=int, default=161)
    parser.add_argument("--level", choices=("noAuthNoPriv", "authNoPriv", "authPriv"),
                        default="authPriv")
    parser.add_argument("--output", default="eaton_snmp_inventory.txt")
    args = parser.parse_args()
    try:
        lines = asyncio.run(collect(args))
    except Exception as exc:
        parser.exit(1, f"SNMP inventory failed: {type(exc).__name__}: {exc}\n")
    Path(args.output).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:4]))
    print(f"\nFull inventory saved to: {Path(args.output).resolve()}")


if __name__ == "__main__":
    main()
