#!/usr/bin/env python3
"""Standalone tester for the Wyze Scale X — no Home Assistant required.

Usage:
  # Watch for advertisements (tests the "scale only beacons when active"
  # hypothesis — step on/off the scale and watch the timestamps):
  python3 scale_tool.py scan [--adapter hci2] [--duration 120]

  # Full sync session: connect, handshake, sync time, list users, drain
  # history, then listen for live weight until idle:
  python3 scale_tool.py sync [--adapter hci2] [--address MAC] [--no-drain]

Run inside a venv with: pip install bleak bleak-retry-connector
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "wyze_scale")
)

from bleak import BleakScanner  # noqa: E402

from wyze_ble import (  # noqa: E402
    LOCAL_NAME,
    SERVICE_UUID,
    Measurement,
    WyzeScaleClient,
)

logging.basicConfig(
    level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logging.getLogger("bleak.backends.bluezdbus.manager").setLevel(logging.INFO)
log = logging.getLogger("scale_tool")


def _is_scale(device, adv) -> bool:
    return (
        SERVICE_UUID in (adv.service_uuids or [])
        or (adv.local_name or device.name) == LOCAL_NAME
    )


async def cmd_scan(args: argparse.Namespace) -> None:
    """Passively watch for scale advertisements and log them."""
    seen: dict[str, int] = {}

    def on_adv(device, adv):
        if not _is_scale(device, adv):
            return
        seen[device.address] = seen.get(device.address, 0) + 1
        mfr = {
            k: v.hex() for k, v in (adv.manufacturer_data or {}).items()
        }
        print(
            f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]} "
            f"{device.address} rssi={adv.rssi} name={adv.local_name!r} "
            f"uuids={adv.service_uuids} mfr={mfr} (#{seen[device.address]})"
        )

    kwargs = {}
    if args.passive:
        # BlueZ passive scanning requires advertisement-monitor patterns
        # (and kernel support); match on the FD7B service UUID byte.
        from bleak.args.bluez import BlueZScannerArgs, OrPattern
        from bleak.assigned_numbers import AdvertisementDataType

        kwargs["bluez"] = BlueZScannerArgs(
            or_patterns=[
                OrPattern(0, AdvertisementDataType.COMPLETE_LOCAL_NAME, b"WL_SC3"),
                OrPattern(
                    0,
                    AdvertisementDataType.INCOMPLETE_LIST_SERVICE_UUID16,
                    b"\x7b\xfd",
                ),
            ]
        )
    scanner = BleakScanner(
        detection_callback=on_adv,
        adapter=args.adapter,
        scanning_mode="passive" if args.passive else "active",
        **kwargs,
    )
    print(
        f"Scanning on {args.adapter} for {args.duration}s "
        f"({'passive' if args.passive else 'active'}). "
        "Step on the scale to see when it starts advertising..."
    )
    async with scanner:
        await asyncio.sleep(args.duration)
    if not seen:
        print("No scale advertisements seen — the scale was likely asleep the whole time.")
    else:
        for addr, count in seen.items():
            print(f"{addr}: {count} advertisements")


def _print_measurement(m: Measurement, kind: str) -> None:
    ts = (
        datetime.fromtimestamp(m.timestamp, timezone.utc).isoformat()
        if m.timestamp
        else "-"
    )
    print(
        f"[{kind}] user={m.user_id_hex[:8]}… state={m.measure_state} ts={ts} "
        f"weight={m.weight_kg:.2f}kg imp={m.impedance} bfp={m.bfp_raw} "
        f"muscle={m.muscle_raw} bone={m.bone_raw} water={m.water_raw} "
        f"protein={m.protein_raw} lbm={m.lbm_raw} vfal={m.vfal} bmr={m.bmr} "
        f"body_age={m.body_age} bmi={m.bmi_raw} batt={m.battery} unit={m.unit}"
    )


async def cmd_sync(args: argparse.Namespace) -> None:
    """Full protocol session against a live scale."""
    print(f"Looking for scale on {args.adapter} (waiting up to {args.wait}s)...")
    if args.address:
        device = await BleakScanner.find_device_by_address(
            args.address, timeout=args.wait, adapter=args.adapter
        )
    else:
        device = await BleakScanner.find_device_by_filter(
            _is_scale, timeout=args.wait, adapter=args.adapter
        )
    if device is None:
        print("Scale not found. Step on it to wake it, then re-run.")
        sys.exit(1)
    print(f"Found {device.name} @ {device.address}; connecting...")

    last_live = [0.0]

    def on_live(m: Measurement) -> None:
        last_live[0] = time.monotonic()
        _print_measurement(m, "LIVE")

    client = WyzeScaleClient(on_live_weight=on_live)
    try:
        await client.connect(device)
        print("Connected; handshake OK")

        # Scale clock = local wall time as epoch seconds
        local_epoch = int(
            datetime.now().replace(tzinfo=timezone.utc).timestamp()
        )
        await client.sync_time(local_epoch)
        print("SYNC_TIME ok")

        users = await client.get_users()
        print(f"USER_LIST_NEW: {len(users)} user(s)")
        for u in users:
            print(
                f"  user={u.user_id_hex} sex={u.sex} age={u.age} "
                f"height={u.height}cm athlete={u.athlete_mode} "
                f"only_weight={u.only_weight} last_weight={u.weight_raw / 100:.2f}kg "
                f"last_impedance={u.last_impedance}"
            )

        if not args.no_drain:
            for u in users:
                print(f"Draining history for {u.user_id_hex[:8]}… "
                      "(WARNING: acknowledged records are deleted from the scale)")
                records = await client.drain_history(u)
                for r in records:
                    _print_measurement(r, "HIST")
                print(f"  {len(records)} record(s)")

        print(f"Listening for live weight for {args.listen}s (step on the scale)...")
        start = time.monotonic()
        while time.monotonic() - start < args.listen and client.is_connected:
            await asyncio.sleep(0.5)
            if last_live[0] and time.monotonic() - last_live[0] > 8:
                print("Live stream idle; stopping")
                break
    finally:
        await client.disconnect()
        print("Disconnected")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="watch for scale advertisements")
    scan.add_argument("--adapter", default="hci2")
    scan.add_argument("--duration", type=int, default=120)
    scan.add_argument("--passive", action="store_true", help="passive scanning mode")

    sync = sub.add_parser("sync", help="connect and run a full session")
    sync.add_argument("--adapter", default="hci2")
    sync.add_argument("--address", help="scale MAC (otherwise discover by name/UUID)")
    sync.add_argument("--wait", type=int, default=60, help="discovery timeout")
    sync.add_argument("--listen", type=int, default=60, help="live-weight listen time")
    sync.add_argument(
        "--no-drain",
        action="store_true",
        help="skip history drain (acknowledging deletes records from the scale)",
    )

    args = parser.parse_args()
    if args.command == "scan":
        asyncio.run(cmd_scan(args))
    else:
        asyncio.run(cmd_sync(args))


if __name__ == "__main__":
    main()
