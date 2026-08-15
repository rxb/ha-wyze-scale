# Wyze Scale — Home Assistant integration

Native Home Assistant BLE integration for the **Wyze Scale X** (`WL_SC3`),
built on the reverse-engineered protocol documented in
[PROTOCOL.md](PROTOCOL.md). No cloud, no Wyze app required.

## Design: battery first

The scale is battery powered, so this integration **never keeps a
connection open and never polls on a schedule**. Instead it:

1. **Listens passively** for the scale's BLE advertisements (free — no
   radio transmission to the scale).
2. **Connects briefly** only when there is a reason to: the scale
   reappears after having been silent (it woke up), its advertisement
   manufacturer data changes, or you press **Poll now**.
3. During a session it syncs the scale's clock, reads the user list,
   drains cached history records, captures any live weigh-in, and
   **disconnects**.
4. All data is persisted in HA storage, so sensors keep their last values
   across restarts and while the scale sleeps.

A sync is triggered when advertisements *resume* after ≥60 s of silence
(the scale waking up), or when the advertisement's manufacturer data
changes — not on every beacon. Automatic syncs are further rate-limited by
a configurable cooldown (default 120 s). Because the test scale was
observed advertising continuously for long stretches (in which case no
wake-up can be detected), a periodic fallback sync (default every 6 h,
0 to disable) guarantees cached history is eventually drained.

> **Important:** reading a history record requires acknowledging it, and
> acknowledging **deletes it from the scale**. Synced measurements exist
> only in Home Assistant afterwards. Data is saved to disk immediately
> after every session, including partial ones.

## Devices and entities

- **Wyze Scale** (main device)
  - `Battery`, `Last sync` (diagnostic sensors)
  - `Poll now` button — forces an immediate connect-and-sync. The scale
    must be awake (advertising) for this to work; BLE cannot wake it.
- **Scale user &lt;id&gt;** (one sub-device per user stored on the scale,
  linked to the main device)
  - `Weight`, `BMI`, `Body fat`, `Muscle mass`, `Bone mass`, `Body water`,
    `Protein`, `Lean body mass`, `Visceral fat level`,
    `Basal metabolic rate`, `Metabolic age`, `Last measurement`,
    `Impedance` (diagnostic)

Sub-devices exist so each person can rename their device and link it to
their Home Assistant person. Users are identified by an opaque 16-byte ID
(shown as the `user_id` attribute on the Weight sensor); the first
measurement will tell you whose is whose.

### Managing users

Profiles can come from the Wyze app, or be managed directly from HA — the
scale must be awake (advertising) for either service:

- **`wyze_scale.add_user`** — creates a profile (sex, age, height and an
  *approximate* weight, which the scale uses to match weigh-ins to users;
  optional athlete/weight-only modes). Returns the generated `user_id`;
  the new sub-device appears immediately.
- **`wyze_scale.delete_user`** — deletes a profile from the scale and
  removes its sub-device. Takes the 32-hex-character `user_id`.

`address` is only needed if more than one scale is configured.

Body-composition sensors are `None` for weight-only profiles or when
impedance could not be measured.

### Measurement events

Sensors only hold the *latest* reading per user, but every synced
measurement (live and history) additionally fires a **`wyze_scale_measurement`**
event on the HA event bus with the full scaled reading, its original
timestamp, `user_id`, `device_address`, and `source` (`live`/`history`).
Because history records are deleted from the scale once synced, this event
is the durable record of every weigh-in — use it in automations or to log
multiple weigh-ins that happen between syncs.

### Field scaling

Verified against a real measurement: weight is kg × 100; body fat, water
and protein are percent × 10; muscle mass, lean body mass and bone mass
are kg × 10; BMI is × 10; impedance (Ω), visceral fat level, BMR (kcal)
and metabolic age are unscaled. The verifying frame satisfied
`lbm = weight × (1 − bfp)` and `muscle = lbm − bone` exactly.

## Installation

Copy `custom_components/wyze_scale` into your HA `config/custom_components/`
(or add this repo to HACS as a custom repository), restart HA. The scale is
auto-discovered when it advertises (service UUID `0000FD7B` / name
`WL_SC3`); otherwise add it via *Settings → Devices & Services → Add
Integration → Wyze Scale* while someone is standing on it.

Options (on the integration entry):

| Option | Default | Meaning |
|---|---|---|
| Sync automatically when the scale wakes up | on | advertisement-triggered syncs |
| Minimum seconds between automatic syncs | 120 | cooldown for automatic syncs |
| Also sync every N seconds | 21600 | periodic fallback sync; 0 disables |
| Unit shown on the scale display | none | push kg/lb to the display on sync |

## Standalone testing

`scripts/scale_tool.py` exercises the protocol without Home Assistant:

```bash
python3 -m venv .venv && .venv/bin/pip install bleak bleak-retry-connector
# Characterize advertising behavior (step on/off the scale, watch timestamps):
.venv/bin/python scripts/scale_tool.py scan --adapter hci2 --duration 120
# Full session: handshake, time sync, user list, history drain, live weight:
.venv/bin/python scripts/scale_tool.py sync --adapter hci2
# Manage user profiles:
.venv/bin/python scripts/scale_tool.py add-user --adapter hci2 \
    --sex m --age 40 --height 180 --weight 80
.venv/bin/python scripts/scale_tool.py del-user --adapter hci2 --user-id <hex>
```

`sync` drains (and therefore deletes) pending history records — use
`--no-drain` to leave them on the scale.

Protocol unit tests: `.venv/bin/pip install pytest xxtea && .venv/bin/python -m pytest tests/`.

## Protocol library

`custom_components/wyze_scale/wyze_ble/` is a self-contained asyncio client
(bleak + bleak-retry-connector only): XXTEA cipher, DH key exchange,
framing, and command layer. It can be reused outside Home Assistant.
