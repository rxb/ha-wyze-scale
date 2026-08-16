# Development

Developer documentation for the Wyze Scale integration. User-facing
documentation is in [README.md](README.md); the full reverse-engineered BLE
protocol specification is in [PROTOCOL.md](PROTOCOL.md).

## Repository layout

```
custom_components/wyze_scale/
├── __init__.py        # setup, add_user/delete_user actions
├── config_flow.py     # Bluetooth discovery + options flow
├── coordinator.py     # sync sessions, triggers, persistence, data model
├── sensor.py          # scale + per-user sensors (dynamic sub-devices)
├── button.py          # Poll now
├── services.yaml      # action UI metadata
└── wyze_ble/          # standalone protocol library (no HA imports)
    ├── xxtea.py       # XXTEA cipher, 8-byte-block ECB variant
    ├── protocol.py    # framing, key exchange, message build/parse
    └── client.py      # asyncio BLE client (bleak + bleak-retry-connector)
scripts/scale_tool.py  # standalone scan/sync/user-management tester
tests/test_protocol.py # protocol unit tests
```

## Architecture

### Battery-first design

The scale sleeps with its radio off unless someone is using it, and BLE
cannot wake it. The coordinator therefore never keeps a connection open,
and connects only when a trigger fires:

1. **Advertisement events** - a callback registered with HA's Bluetooth
   manager. HA deduplicates advertisements, so the callback fires only when
   the scale (re)appears or its advertisement content changes - both are
   wake-up/activity signals. Rate-limited by the configurable cooldown.
2. **Periodic fallback** (`update_interval`, default 6 h) - a catch-up in
   case no wake-up was detectable (the test scale was observed advertising
   continuously for long stretches, possibly kept awake by active
   scanning). Skipped silently when the scale isn't advertising.
3. **Poll now** button / user-management actions - explicit, and the only
   triggers that surface "scale not reachable" as an error.

### Sync session

One session (`coordinator._async_sync_session`): connect → DH key exchange →
`SYNC_TIME` → optional `SET_UNIT` → `USER_LIST_NEW` → per user:
`CURRENT_USER_NEW` + drain/ack `HISTORY_WEIGHT_DATA` → linger while live
`CUR_WEIGHT_DATA` frames stream (grace 5 s, idle 8 s, cap 90 s) → disconnect.

### Data durability

Acknowledging a history record **deletes it from the scale**, so the write
path is ordered for durability:

- `client.drain_history()` hands each record to a callback **before**
  sending the ack.
- The callback (`coordinator._record_measurement`) fires the
  `wyze_scale_measurement` event (the permanent record - sensors only keep
  the latest reading per user), updates entity state, and queues a debounced
  save to HA storage (`Store`, one file per config entry).
- A final save runs in the session's `finally`, including on failures.

State is restored from the store at setup; the integration never connects
during `async_setup_entry` (the scale would normally be asleep and
unreachable anyway).

### Per-user sub-devices

Users are exposed as sub-devices via `via_device`. Entity platforms add
entities dynamically: a coordinator listener checks for unseen user IDs on
every update and adds their sensors, so users created mid-flight (first
weigh-in, `add_user` action) appear without a reload.

### Timestamps

The scale's clock is local wall time encoded as epoch seconds.
`_utc_to_device_epoch` / `_device_epoch_to_utc` in `coordinator.py` convert
in both directions (SYNC_TIME out, history timestamps in).

### Field scaling

Verified against a real measurement: weight is kg × 100; body fat, water and
protein are percent × 10; muscle mass, lean body mass and bone mass are
kg × 10; BMI is × 10; impedance (Ω), visceral fat level, BMR (kcal) and
metabolic age are unscaled. The verifying frame satisfied
`lbm = weight × (1 − bfp)` and `muscle = lbm − bone` exactly. Scaling lives
in `Measurement` properties in `wyze_ble/protocol.py`.

## Protocol library

`custom_components/wyze_scale/wyze_ble/` is self-contained (depends only on
`bleak` and `bleak-retry-connector`) and reusable outside Home Assistant:

```python
from wyze_ble import WyzeScaleClient

client = WyzeScaleClient(on_live_weight=print)
await client.connect(ble_device)   # includes key exchange
await client.sync_time(local_epoch)
users = await client.get_users()
for user in users:
    await client.drain_history(user, on_record=persist)  # acks DELETE records
await client.disconnect()
```

See PROTOCOL.md for the wire format: single GATT characteristic, 4-bit
rolling frame counter, 32-bit Diffie-Hellman key exchange (obfuscation, not
security), XXTEA in independent 8-byte blocks, and the command layer.

## Standalone test tool

`scripts/scale_tool.py` exercises the protocol without Home Assistant:

```bash
python3 -m venv .venv && .venv/bin/pip install bleak bleak-retry-connector

# Watch advertisements (step on/off the scale, watch the timestamps):
.venv/bin/python scripts/scale_tool.py scan --adapter hci2 --duration 120

# Full session: handshake, time sync, user list, history drain, live weight:
.venv/bin/python scripts/scale_tool.py sync --adapter hci2

# User management:
.venv/bin/python scripts/scale_tool.py add-user --adapter hci2 \
    --sex m --age 40 --height 180 --weight 80
.venv/bin/python scripts/scale_tool.py del-user --adapter hci2 --user-id <hex>
```

`sync` drains (and therefore **deletes**) pending history records - use
`--no-drain` to leave them on the scale. `--adapter` selects the HCI device;
`--address` skips discovery.

## Tests

```bash
.venv/bin/pip install pytest xxtea
.venv/bin/python -m pytest tests/
```

`tests/test_protocol.py` covers the XXTEA cipher (including a cross-check
against the reference `xxtea` package), key derivation, DH exchange, frame
round-trips, request layouts, user records, and live/history message parsing
with the verified field scalings (synthetic values).

For import-checking the HA-facing modules against a real Home Assistant,
install `homeassistant` (plus its Bluetooth deps: `aiousbwatcher`,
`habluetooth`, `bluetooth-adapters`, `bluetooth-auto-recovery`,
`bluetooth-data-tools`, `pyserial`, `pyudev`) into the venv and import
`custom_components.wyze_scale.*` from the repo root.

## Hardware verification status

Verified against a real Scale X: handshake, SYNC_TIME, USER_LIST_NEW with
multiple users (the byte at offset 6 is the record count, not a success
flag), CURRENT_USER_NEW acks, spontaneous HISTORY_WEIGHT_DATA delivery
after user selection, the live `0x08` stream (including the zeroed user ID
+ `measure_state` 0→1→2 progression), and all field scalings.

Heart rate: decoded but deferred (maybe-in-the-future). Protocol from the
decompiled `com.wyze.pluto` app: `HEART_MODE` (0x10, no args) enters
measurement mode and is re-sent to keep it alive; `HEART_RESULT` (0x11)
streams [on_scale, measure_state, bpm] with measure_state 1 = complete;
`WEIGHT_MODE` (0x12, no args) restores normal weighing. See PROTOCOL.md
§5.13. The library has `build_heart_mode`/`parse_heart_result` and
`scale_tool.py heartrate` for future work.

Hardware findings (WL_SC3): sending the correct no-arg HEART_MODE **does**
switch the scale out of weight mode (the `0x08` weight stream stops), so
the command is accepted, but no `0x11` result was ever produced in
testing. The app's flow (`WplHeartRateHomeActivity`) selects a current
user and drives an interactive on-screen "stand still / hold" session with
step states; fully reproducing that (current-user context, settled weight,
sustained stillness) is what's missing. This interaction model doesn't fit
a passive background integration, which is why it's deferred rather than
wired in. My original probe also failed only because it appended a
spurious argument byte to 0x10.

Not yet exercised on hardware: the history ack/delete flow (records were
delivered but deliberately left unacked), multi-message user lists, and
`add_user` / `delete_user`.
