# Development

Developer documentation for the Wyze Scale integration. User-facing
documentation is in [README.md](README.md); the full reverse-engineered BLE
protocol specification is in [PROTOCOL.md](PROTOCOL.md).

## Repository layout

```
custom_components/wyze_scale/
├── __init__.py        # entry setup, reload-on-change listener
├── config_flow.py     # Bluetooth discovery, options, user subentry flow
├── coordinator.py     # sync sessions, triggers, persistence, reconciliation
├── users.py           # pure profile model + scale<->HA reconcile logic
├── sensor.py          # scale sensors + per-subentry user sensors
├── button.py          # Poll now
├── brand/             # icon.png (+@2x); local brand images (transparent)
└── wyze_ble/          # standalone protocol library (no HA imports)
    ├── xxtea.py       # XXTEA cipher, 8-byte-block ECB variant
    ├── protocol.py    # framing, key exchange, message build/parse
    └── client.py      # asyncio BLE client (bleak + bleak-retry-connector)
scripts/scale_tool.py  # standalone scan/sync/user-management tester
tests/test_protocol.py # protocol unit tests
tests/test_users.py    # reconciliation unit tests
```

## Brand images

`custom_components/wyze_scale/brand/` holds the integration icon (`icon.png`
+ `icon@2x.png`, 256/512 px, transparent background). Home Assistant's
[brands proxy](https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api/)
serves local `brand/` images directly and prefers them over the CDN, so no
submission to the `home-assistant/brands` repo is needed. Derived from Wyze's
official Scale X product image, trimmed and centered on transparency; HA
falls back to `icon.png` for dark mode (no separate `dark_icon`).

## Scale users as config subentries

Each scale user is a config **subentry** of the scale's config entry, so
they're managed in the UI (add / edit / delete on the scale's device page),
scoped per scale. See `config_flow.WyzeScaleUserSubentryFlow`.

- **Desired state** lives in the subentries (profile: name, sex, age,
  height, approx weight, athlete/weight-only).
- **Measurements** live in the coordinator/store keyed by `user_id` and feed
  the sensors. `sensor.async_setup_entry` iterates `entry.subentries` and
  adds one sensor set per user with `config_subentry_id`.
- **Reconciliation** (`users.reconcile`, pure + unit tested) runs inside the
  sync session after `get_users`: it pushes HA-side creates/updates/deletes
  to the scale and imports scale-side users (e.g. Wyze-app-created) as new
  subentries via `async_add_subentry`.
- **Deletions** are detected on load (a previously-known subentry is gone)
  and recorded as tombstones so the next sync deletes the user from the
  scale and doesn't re-import it. The coordinator persists `tombstones`,
  `pushed_profiles` (last-pushed scale fields, for drift detection), and
  `known_subentries` alongside the measurement data.
- Any subentry or option change fires the entry update listener, which
  reloads the entry (serialized on `setup_lock`) to rebuild entities.

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

Each user's sensors live on a sub-device (`via_device` the scale) tied to
its config subentry via `config_subentry_id`. New/edited/removed users are
picked up on entry reload (see the subentry section above), not via a
dynamic add-listener.

### Timestamps

The scale's clock is standard Unix time (UTC), like the Wyze app (which
sends `System.currentTimeMillis()/1000`). `_utc_to_device_epoch` /
`_device_epoch_to_utc` in `coordinator.py` are plain UTC epoch conversions
(SYNC_TIME out, history timestamps in) with no timezone shifting - doing a
local-time conversion here caused last-weigh-in times to read hours in the
future.

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
.venv/bin/pip install -r requirements-test.txt
.venv/bin/python -m pytest tests/
```

`tests/test_protocol.py` covers the XXTEA cipher (including a cross-check
against the reference `xxtea` package), key derivation, DH exchange, frame
round-trips, request layouts, user records, and live/history message parsing
with the verified field scalings (synthetic values). `tests/test_users.py`
covers the pure profile/reconciliation logic.

The Home Assistant-level tests use
[`pytest-homeassistant-custom-component`](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component)
(which pins a full Home Assistant and mocked Bluetooth stack):
`tests/test_config_flow.py` (discovery, manual setup, options, user
subentries), `tests/test_init.py` (setup/unload, diagnostics), and
`tests/test_coordinator.py` (a sync session against a fake BLE client).

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

Not yet exercised on hardware/live HA: the history ack/delete flow (records
were delivered but deliberately left unacked), multi-message user lists, and
the full subentry user-reconciliation path (create/update/delete on the
scale, import as subentries). The pure reconcile logic has unit tests
(`tests/test_users.py`); the BLE and HA-wiring side needs a live instance.


## Scale Ultra implementation and validation

Ultra entries retain the advertised model in config. Older entries can be
identified from current Bluetooth service information. The coordinator routes
`WL_SCU` to a separate live-weight session; the original Scale X session is
unchanged. `wyze_ble/ultra.py` owns framing, profile-list validation and pure
weight assignment. Ultra writes use `response=False`; the Scale X client
retains `response=True`.

The Ultra command allowlist is time sync (0x01) and profile list (0x18).
There are no history acknowledgements or profile writes. A valid observed
single-message profile list is required before accepting buffered completions.
Nonzero unknown IDs, unfinished frames, out-of-range weights and ambiguous
matches are rejected. Known IDs do not determine the person; the unique
reference-weight match does. References are stored separately from historical
sensor values to avoid seeding from an older misassigned reading.

`ultra_wakeup.py` observes HCI legacy connectable advertisements: four target
reports within one second trigger activity; a gap over 1.5 seconds rearms it.
The existing cooldown and sync lock still apply. `ultra_timing.py` temporarily
loads target-specific connection parameters through Linux management sockets.
It observes and restores the original (7,9,0,800) tuple; the tested temporary
tuple is (24,36,2,500). Restoration is attempted even after a lost command ACK.
All native socket operations require an appropriately configured Linux host.

Hardware validation: one Ultra on a Raspberry Pi 4; automatic live collection
for two people, socks/weight-only readings, completion states 2/3/4, empty or
primary profile IDs, and saved adaptive references. The generalized checkout
must still be tested on hardware before a release. Other adapters, proxies,
firmware, body composition and offline history are not validated.

`tests/test_ultra.py` uses synthetic profiles and HCI packets, including
device isolation, restoration, framing, write mode, buffered completion,
assignment, reference persistence and bounded fresh-connection retries.
No personal captures are required or distributed.

The recorded test environment used Python 3.14 and Home Assistant 2026.10.0b0
(the version pinned by the test plugin). These are development dependencies,
not a new minimum Home Assistant requirement for Scale X.
