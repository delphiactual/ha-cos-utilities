"""Standalone smoke test for the CSU API client.

Reads CSU_USERNAME / CSU_PASSWORD from the environment, logs in, lists the
account's services, and prints 3 days of usage per meter so the numbers can be
compared with the CSU portal. Uses the same csu.py the integration ships.

    pip install aiohttp arrow python-dateutil
    python scripts/smoke_test.py [days]
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import types
from collections import defaultdict
from datetime import datetime
from datetime import timedelta
from pathlib import Path

from dateutil import tz

COMPONENT_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "csu"


def load_csu():
    """Import csu.py without running the package __init__ (which needs Home Assistant)."""
    pkg = types.ModuleType("csu_pkg")
    pkg.__path__ = [str(COMPONENT_DIR)]
    sys.modules["csu_pkg"] = pkg
    return importlib.import_module("csu_pkg.csu"), importlib.import_module(
        "csu_pkg.const"
    )


def mask(value: str) -> str:
    return "***" + str(value)[-4:]


def print_reads(meter_type: str, reads, unit: str) -> None:
    """Print each read plus per-day totals."""
    exported = meter_type == "ELEC"
    header = f"{'start (local)':<22}{'import ' + unit:>14}"
    if exported:
        header += f"{'export ' + unit:>14}"
    print(header)
    daily = defaultdict(lambda: [0.0, 0.0])
    for read in reads:
        line = (
            f"{read.start_time.strftime('%Y-%m-%d %H:%M'):<22}{read.consumption:>14.3f}"
        )
        if exported:
            line += f"{read.received:>14.3f}"
        print(line)
        day = read.start_time.strftime("%Y-%m-%d")
        daily[day][0] += read.consumption
        daily[day][1] += read.received
    print("-- daily totals --")
    for day in sorted(daily):
        imp, exp = daily[day]
        line = f"{day:<22}{imp:>14.3f}"
        if exported:
            line += f"{exp:>14.3f}"
        print(line)
    print()


async def main() -> int:
    username = os.environ.get("CSU_USERNAME")
    password = os.environ.get("CSU_PASSWORD")
    if not username or not password:
        print("Set CSU_USERNAME and CSU_PASSWORD in the environment.", file=sys.stderr)
        return 2
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 3

    import aiohttp

    csu, const = load_csu()
    local = tz.gettz(const.TIME_ZONE)
    now = datetime.now(local)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = midnight - timedelta(days=days)

    async with aiohttp.ClientSession() as session:
        api = csu.CSU(session, username, password)
        await api.async_login()
        print(f"Logged in. Customers: {[mask(c.customer_id) for c in api.customers]}")
        await api.async_get_meters()
        print(f"Services ({len(api.meters)}):")
        for m in api.meters:
            print(
                f"  {m.meter_type.name:<6} service={m.service_number:<12} "
                f"meter={mask(m.meter_number)} resolution={m.read_resolution}"
            )
        print()

        units = {"ELEC": "kWh", "GAS": "CCF", "WATER": "ft3"}
        for m in api.meters:
            kind = m.meter_type.name
            # Water is daily-only; everything else is pulled hourly.
            agg = (
                csu.AggregateType.DAY
                if m.read_resolution == csu.ReadResolution.DAY
                else csu.AggregateType.HOUR
            )
            print(f"=== {kind} ({agg}) {start:%Y-%m-%d} to {now:%Y-%m-%d %H:%M} ===")
            reads = await api.async_get_usage_reads(m, agg, start, now)
            if not reads:
                print("(no reads returned)\n")
                continue
            print_reads(kind, reads, units[kind])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
