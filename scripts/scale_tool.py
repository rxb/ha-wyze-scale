#!/usr/bin/env python3
"""Standalone tester for the Wyze Scale X - no Home Assistant required.

Usage:
  # Watch for advertisements (tests the "scale only beacons when active"
  # hypothesis - step on/off the scale and watch the timestamps):
  python3 scale_tool.py scan [--adapter hci2] [--duration 120]

  # Full sync session: connect, handshake, sync time, list users, drain
  # history, then listen for live weight until idle:
  python3 scale_tool.py sync [--adapter hci2] [--address MAC] [--no-drain]

  # Create a user profile on the scale (prints the generated user id):
  python3 scale_tool.py add-user --sex m --age 40 --height 180 --weight 80

  # Delete a user profile:
  python3 scale_tool.py del-user --user-id <32 hex chars>

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
    UserRecord,
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
        print("No scale advertisements seen - the scale was likely asleep the whole time.")
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


async def _connect_session(
    args: argparse.Namespace,
    on_live=None,
    attempts: int = 3,
) -> WyzeScaleClient:
    """Find the scale, connect, handshake, and sync its clock.

    Retries the connect/handshake/sync a few times: the scale can be
    half-awake right after it starts advertising and drop the first
    command (a marginal BLE adapter makes this worse).
    """
    from wyze_ble import WyzeScaleError

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
    print(f"Found {device.name} @ {device.address}")

    for attempt in range(1, attempts + 1):
        client = WyzeScaleClient(on_live_weight=on_live)
        try:
            print(f"Connecting (attempt {attempt}/{attempts})...")
            await client.connect(device)
            print("Connected; handshake OK")
            # Scale clock = standard Unix time (UTC), like the Wyze app.
            await client.sync_time(int(datetime.now(timezone.utc).timestamp()))
            print("SYNC_TIME ok")
            return client
        except (WyzeScaleError, Exception) as err:  # noqa: BLE001
            await client.disconnect()
            print(f"  connect attempt {attempt} failed: {err}")
            if attempt == attempts:
                print("Giving up. Make sure you're standing on the scale, then retry.")
                sys.exit(1)
            await asyncio.sleep(2)


def _print_users(users: list[UserRecord]) -> None:
    print(f"USER_LIST_NEW: {len(users)} user(s)")
    for u in users:
        print(
            f"  user={u.user_id_hex} sex={u.sex} age={u.age} "
            f"height={u.height}cm athlete={u.athlete_mode} "
            f"only_weight={u.only_weight} last_weight={u.weight_raw / 100:.2f}kg "
            f"last_impedance={u.last_impedance}"
        )


async def cmd_sync(args: argparse.Namespace) -> None:
    """Full protocol session against a live scale."""
    last_live = [0.0]

    def on_live(m: Measurement) -> None:
        last_live[0] = time.monotonic()
        _print_measurement(m, "LIVE")

    client = await _connect_session(args, on_live=on_live)
    try:
        users = await client.get_users()
        _print_users(users)

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


async def cmd_watch(args: argparse.Namespace) -> None:
    """Protocol exploration: send optional commands, then dump all traffic.

    Every decrypted message from the scale is printed with its full hex,
    including unknown command IDs. Intended for reverse engineering, e.g.
    the heart rate commands (HEART_MODE 0x10, HEART_RESULT 0x11,
    WEIGHT_MODE 0x12).
    """
    from wyze_ble import protocol

    def on_message(msg) -> None:
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"{ts} <- cmd=0x{msg.cmd:02x} status={msg.status} "
              f"len={len(msg.raw)} hex={msg.raw.hex()}")
        if msg.cmd == protocol.CMD_CUR_WEIGHT_DATA:
            try:
                _print_measurement(protocol.parse_live_weight(msg), "LIVE")
            except protocol.ProtocolError:
                pass

    client = await _connect_session(args)
    client._on_message = on_message  # noqa: SLF001 - exploration tool
    try:
        if args.select_first:
            users = await client.get_users()
            _print_users(users)
            if users:
                await client.select_user(users[0])
                print(f"Selected user {users[0].user_id_hex[:8]}...")
        for spec in args.send or []:
            cmd_str, _, arg_str = spec.partition(":")
            cmd_id = int(cmd_str, 16)
            cmd_args = bytes.fromhex(arg_str) if arg_str else b""
            print(f"-> cmd=0x{cmd_id:02x} args={cmd_args.hex() or '(none)'}")
            await client.send_raw(cmd_id, cmd_args)
            await asyncio.sleep(1.5)
        print(f"Watching for {args.duration}s; use the scale now...")
        start = time.monotonic()
        while time.monotonic() - start < args.duration and client.is_connected:
            await asyncio.sleep(0.5)
    finally:
        await client.disconnect()
        print("Disconnected")


async def cmd_heartrate(args: argparse.Namespace) -> None:
    """Measure heart rate, replicating the official app's BLE flow.

    Sends HEART_MODE (0x10, no args), keeps it alive while measuring, and
    prints HEART_RESULT (0x11) frames. Stand on the scale barefoot and
    hold still. Restores normal weighing mode (WEIGHT_MODE 0x12) on exit.
    """
    done = asyncio.Event()
    result = {"bpm": None}

    def on_heart(hr) -> None:
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"{ts} HEART on_scale={hr.on_scale} state={hr.measure_state} "
              f"bpm={hr.bpm}")
        if hr.is_complete:
            result["bpm"] = hr.heart_rate
            done.set()

    def on_message(msg) -> None:
        if msg.cmd == 0x10:
            print(f"   HEART_MODE ack: status={msg.status}")

    from wyze_ble import WyzeScaleError

    client = await _connect_session(args)
    client._on_heart_result = on_heart  # noqa: SLF001 - exploration tool
    client._on_message = on_message  # noqa: SLF001
    try:
        # Enter heart mode IMMEDIATELY: the scale sleeps within a few
        # seconds of thinking you're done, so don't spend the awake window
        # on other commands. Stay on the scale the whole time.
        print("Entering heart-rate mode NOW; stay on the scale barefoot and hold still...")
        start = time.monotonic()
        while not done.is_set() and time.monotonic() - start < args.duration:
            if not client.is_connected:
                print("Scale disconnected (it sleeps when it thinks you've "
                      "stepped off). Stay on it and keep still next time.")
                break
            try:
                await client.enter_heart_mode()  # app re-sends periodically
            except WyzeScaleError as err:
                print(f"Lost the scale mid-measurement: {err}")
                break
            await asyncio.sleep(1.5)
        if result["bpm"]:
            print(f"\n*** Heart rate: {result['bpm']} bpm ***")
        elif not done.is_set():
            print("\nNo completed heart-rate result (timed out or disconnected).")
    finally:
        try:
            if client.is_connected:
                await client.enter_weight_mode()
        except WyzeScaleError:
            pass
        finally:
            await client.disconnect()
            print("Disconnected")


async def cmd_add_user(args: argparse.Namespace) -> None:
    """Create a new user profile on the scale (PROTOCOL.md §5.9)."""
    import secrets

    record = UserRecord(
        user_id=secrets.token_bytes(16),
        weight_raw=round(args.weight * 100),
        sex=1 if args.sex == "m" else 0,
        age=args.age,
        height=args.height,
        athlete_mode=1 if args.athlete else 0,
        only_weight=1 if args.weight_only else 0,
        last_impedance=0,
    )
    client = await _connect_session(args)
    try:
        await client.select_user(record)
        print("CURRENT_USER_NEW ok")
        await client.update_user(record)
        print("UPDATE_USER ok")
        print(f"Created user: {record.user_id_hex}")
        _print_users(await client.get_users())
    finally:
        await client.disconnect()
        print("Disconnected")


async def cmd_del_user(args: argparse.Namespace) -> None:
    """Delete a user profile from the scale."""
    user_id = bytes.fromhex(args.user_id)
    if len(user_id) != 16:
        print("user-id must be 32 hex characters (16 bytes)")
        sys.exit(1)
    client = await _connect_session(args)
    try:
        await client.delete_user(user_id)
        print(f"Deleted user: {args.user_id}")
        _print_users(await client.get_users())
    finally:
        await client.disconnect()
        print("Disconnected")


def _add_connect_args(sub_parser: argparse.ArgumentParser) -> None:
    sub_parser.add_argument("--adapter", default="hci2")
    sub_parser.add_argument(
        "--address", help="scale MAC (otherwise discover by name/UUID)"
    )
    sub_parser.add_argument("--wait", type=int, default=60, help="discovery timeout")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="watch for scale advertisements")
    scan.add_argument("--adapter", default="hci2")
    scan.add_argument("--duration", type=int, default=120)
    scan.add_argument("--passive", action="store_true", help="passive scanning mode")

    sync = sub.add_parser("sync", help="connect and run a full session")
    _add_connect_args(sync)
    sync.add_argument("--listen", type=int, default=60, help="live-weight listen time")
    sync.add_argument(
        "--no-drain",
        action="store_true",
        help="skip history drain (acknowledging deletes records from the scale)",
    )

    add_user = sub.add_parser("add-user", help="create a user profile on the scale")
    _add_connect_args(add_user)
    add_user.add_argument("--sex", choices=["m", "f"], required=True)
    add_user.add_argument("--age", type=int, required=True, help="years")
    add_user.add_argument("--height", type=int, required=True, help="centimeters")
    add_user.add_argument(
        "--weight",
        type=float,
        required=True,
        help="approximate weight in kg (used by the scale to match weigh-ins)",
    )
    add_user.add_argument("--athlete", action="store_true", help="athlete mode")
    add_user.add_argument(
        "--weight-only", action="store_true", help="skip body composition"
    )

    del_user = sub.add_parser("del-user", help="delete a user profile from the scale")
    _add_connect_args(del_user)
    del_user.add_argument("--user-id", required=True, help="32 hex characters")

    heartrate = sub.add_parser(
        "heartrate", help="measure heart rate (BLE flow from the official app)"
    )
    _add_connect_args(heartrate)
    heartrate.add_argument(
        "--select-first",
        action="store_true",
        help="select the first stored user before measuring",
    )
    heartrate.add_argument("--duration", type=int, default=90, help="max wait")

    watch = sub.add_parser(
        "watch", help="protocol exploration: send raw commands, dump all traffic"
    )
    _add_connect_args(watch)
    watch.add_argument(
        "--send",
        action="append",
        metavar="CMD[:HEXARGS]",
        help="send a raw command after connecting, e.g. --send 10:01 "
        "(repeatable, sent in order)",
    )
    watch.add_argument(
        "--select-first",
        action="store_true",
        help="select the first stored user before sending/watching",
    )
    watch.add_argument("--duration", type=int, default=180, help="watch time")

    args = parser.parse_args()
    handler = {
        "scan": cmd_scan,
        "sync": cmd_sync,
        "add-user": cmd_add_user,
        "del-user": cmd_del_user,
        "watch": cmd_watch,
        "heartrate": cmd_heartrate,
    }[args.command]
    asyncio.run(handler(args))


if __name__ == "__main__":
    main()
