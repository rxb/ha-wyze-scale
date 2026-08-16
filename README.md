# Wyze Scale for Home Assistant

[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![GitHub Release](https://img.shields.io/github/v/release/Bo-Louvier/ha-wyze-scale)](https://github.com/Bo-Louvier/ha-wyze-scale/releases)

A custom Home Assistant integration for the **Wyze Scale X** smart scale via
Bluetooth Low Energy.

Fully local - no cloud, no Wyze account, and no Wyze app required. Weigh-ins
land in Home Assistant automatically, including measurements taken while
Home Assistant wasn't listening (the scale stores them and they're collected
on the next sync).

---

## Features

- **Automatic weigh-in collection** - the integration notices when the scale
  wakes up, connects, and pulls the new measurement
- **Offline catch-up** - measurements taken while HA was off or out of range
  are stored on the scale and synced later, with their original timestamps
- **A device per person** - every user profile on the scale appears as its
  own sub-device, so each person can rename theirs and link it to their
  Home Assistant person
- **Full body composition** - weight, BMI, body fat, muscle mass, bone mass,
  body water, protein, lean body mass, visceral fat, BMR, and metabolic age
- **Battery friendly** - never holds a connection open; syncs are triggered
  by the scale waking up, with only an occasional catch-up check (default
  every 6 hours, configurable) that skips silently if the scale is asleep
- **User management from HA** - create and delete scale user profiles with
  actions, no Wyze app needed
- **Auto-discovery** - HA detects the scale over Bluetooth automatically
- **Survives restarts** - all readings are stored in HA, so nothing goes
  unavailable while the scale sleeps

---

## Devices and entities

### Wyze Scale (main device)

| Entity | Description |
|--------|-------------|
| Poll now | Button - force an immediate sync (the scale must be awake) |
| Battery | Scale battery level *(diagnostic)* |
| Last sync | When the last successful sync finished *(diagnostic)* |

### Scale user (one sub-device per person)

| Sensor | Unit | Description |
|--------|------|-------------|
| Weight | kg | Most recent weight |
| BMI | - | Body mass index |
| Body fat | % | Body fat percentage |
| Muscle mass | kg | Skeletal muscle mass |
| Bone mass | kg | Bone mass |
| Body water | % | Total body water |
| Protein | % | Protein percentage |
| Lean body mass | kg | Weight minus body fat |
| Visceral fat level | - | Visceral fat rating |
| Basal metabolic rate | kcal | Resting daily energy use |
| Metabolic age | - | Body age estimate |
| Last measurement | timestamp | When this person last weighed in |
| Impedance | Ω | Raw bio-impedance reading *(diagnostic)* |

Weight sensors are in kilograms internally; Home Assistant converts them for
display according to your unit system, and the display unit on the scale
itself is configurable (see Options).

Body-composition sensors are empty for weight-only profiles or when the
scale couldn't measure impedance (e.g. weighing with socks on).

> **Which user is which?** Scale users are identified by an anonymous ID, so
> new sub-devices are named like "Scale user A1B2C3". Weigh yourself once,
> see which device updated, and rename it.

---

## Actions

| Action | Description |
|--------|-------------|
| `wyze_scale.add_user` | Create a user profile on the scale (sex, age, height, and approximate weight - the scale matches weigh-ins to the closest profile). The new person appears as a sub-device immediately. |
| `wyze_scale.delete_user` | Delete a user profile from the scale and remove its sub-device. |

Both actions need the scale to be awake - step on it first. The `address`
field is only needed if you have more than one scale.

## Events

Sensors always show each person's *latest* measurement. In addition, every
collected weigh-in fires a **`wyze_scale_measurement`** event containing the
full reading, its original timestamp, and the user ID - so multiple
weigh-ins between syncs are never lost. Use it in automations:

```yaml
triggers:
  - trigger: event
    event_type: wyze_scale_measurement
actions:
  - action: notify.mobile_app_phone
    data:
      message: >-
        New weigh-in: {{ trigger.event.data.weight_kg }} kg
        ({{ trigger.event.data.source }})
```

---

## Requirements

- **Home Assistant** with Bluetooth set up - either:
  - a Bluetooth adapter on the HA host (built-in or USB), **or**
  - an [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html) within range of the scale
- A **Wyze Scale X** (the model that advertises as `WL_SC3`)

The scale does **not** need to be set up in the Wyze app first - user
profiles can be created directly from Home Assistant.

---

## Installation

### Via HACS (recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Bo-Louvier&repository=ha-wyze-scale&category=Integration)

Or manually: HACS → three-dot menu (⋮) → **Custom repositories** → add
`https://github.com/Bo-Louvier/ha-wyze-scale` with category **Integration**,
then download **Wyze Scale** and restart Home Assistant.

### Manual

1. Download the [latest release](https://github.com/Bo-Louvier/ha-wyze-scale/releases/latest)
2. Copy `custom_components/wyze_scale/` into your HA `config/custom_components/` directory
3. Restart Home Assistant

---

## Configuration

### Automatic discovery

Step on the scale (it only transmits while awake). A discovered **Wyze
Scale** appears under **Settings → Devices & Services** - click
**Configure** and confirm.

### Manual setup

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **Wyze Scale**
3. Pick your scale from the list (step on it first if the list is empty)

### Options

Click **Configure** on the integration entry to adjust:

| Option | Default | Description |
|--------|---------|-------------|
| Sync automatically when the scale wakes up | on | Connect when the scale starts transmitting |
| Minimum seconds between automatic syncs | 120 | Rate limit for automatic syncs - protects the scale's battery |
| Also sync every N seconds | 21600 (6 h) | Periodic catch-up sync in case a wake-up went unnoticed; 0 disables |
| Unit shown on the scale display | none | Push kg or lb to the scale's display (doesn't affect HA units) |

---

## How syncing works

The scale is battery powered and spends nearly all its time asleep with its
radio off. This integration is built around that:

1. Home Assistant **listens passively** for the scale's Bluetooth
   advertisements - this costs the scale nothing.
2. When the scale wakes up (someone stepped on it), the integration
   **connects briefly**, collects any stored measurements and the live
   weigh-in, and **disconnects**.
3. As a safety net, a **periodic catch-up sync** (default every 6 hours,
   configurable, 0 to disable) collects anything a missed wake-up left
   behind. It checks whether the scale is transmitting first and skips
   silently if it's asleep.
4. Everything is saved in Home Assistant, so entities keep their values
   while the scale sleeps and across HA restarts.

> **Note:** collected measurements are removed from the scale's internal
> memory as they're synced (this is how the scale's protocol works - the
> Wyze app does the same). After a sync, Home Assistant holds the only copy.

---

## Known limitations

- **The scale can't be woken remotely.** Bluetooth can't turn it on - *Poll
  now* and the user-management actions only work while the scale is awake
  (shortly after someone steps on it).
- **One connection at a time.** While the Wyze app is connected to the
  scale, this integration can't sync, and vice versa.
- **Weigh-ins are matched by weight.** Like the Wyze app, the scale assigns
  a measurement to the stored profile with the closest weight. Two users
  with similar weights may get misattributed readings.
- **No heart rate (maybe in the future).** The scale can measure heart rate
  when you stand on it barefoot, and the BLE protocol for it has been worked
  out, but it needs an interactive "stay on the scale and hold still"
  session that doesn't fit a passive background integration well. It may be
  added later; for now it's out of scope.

---

## Troubleshooting

### "Scale is not reachable" when pressing Poll now

The scale is asleep. Step on it to wake it, then press the button again
(or just let the automatic sync handle it).

### No devices found during setup

The scale only transmits while awake - step on it, then retry the setup.
Also confirm HA's Bluetooth integration works (Settings → Devices &
Services → Bluetooth) and the scale is within range of the adapter/proxy.

### A weigh-in didn't show up

- Check the **Last sync** sensor - if it's stale, HA never noticed the
  wake-up; move the Bluetooth adapter/proxy closer to the scale.
- The measurement isn't lost: it's stored on the scale and will arrive with
  its original timestamp on the next successful sync (press **Poll now**
  while standing on the scale to force one).

### Debug logging

Add to `configuration.yaml` and restart (or use the integration's **Enable
debug logging** menu item for a temporary session):

```yaml
logger:
  logs:
    custom_components.wyze_scale: debug
```

---

## Contributing

Bug reports and pull requests are welcome - please open an
[issue](https://github.com/Bo-Louvier/ha-wyze-scale/issues) first for large
changes.

Developer documentation - architecture, the reverse-engineered BLE protocol,
the standalone test tool, and how to run the tests - is in
[DEVELOPMENT.md](DEVELOPMENT.md). The full protocol specification is in
[PROTOCOL.md](PROTOCOL.md).

---

## Acknowledgements

Thanks to **Ben Gruver** for reverse engineering the Wyze Scale X BLE
protocol in [wyze_scale_tool](https://github.com/JesusFreke/wyze_scale_tool),
which made this integration possible.
