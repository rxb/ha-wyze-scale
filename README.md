# Wyze Scale for Home Assistant

[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![GitHub Release](https://img.shields.io/github/v/release/Bo-Louvier/ha-wyze-scale)](https://github.com/Bo-Louvier/ha-wyze-scale/releases)

> **Disclaimer:** This is a third-party integration, unaffiliated with Wyze
> Labs, Inc. and created without their knowledge or approval. "Wyze" and any
> Wyze logos or product images are used purely for descriptive and
> identification purposes and do not imply any affiliation with, or
> authorization, sponsorship, or endorsement by, Wyze.

A custom Home Assistant integration for the **Wyze Scale X** smart scale via
Bluetooth Low Energy.

Fully local - no cloud, no Wyze account, and no Wyze app required. Weigh-ins
land in Home Assistant automatically, including measurements taken while
Home Assistant wasn't listening (the scale stores them and they're collected
on the next sync).

---

## Features

- **Automatic weigh-in collection** - the integration connects shortly after
  a weigh-in and pulls the new measurement
- **Offline catch-up** - measurements taken while HA was off or out of range
  are stored on the scale and synced later, with their original timestamps
- **A device per person** - every user profile on the scale appears as its
  own sub-device, so each person can rename theirs and link it to their
  Home Assistant person
- **Full body composition** - weight, BMI, body fat, muscle mass, bone mass,
  body water, protein, lean body mass, visceral fat, BMR, and metabolic age
- **Battery friendly** - never holds a connection open and never polls on a
  fixed schedule; it connects when the scale appears over Bluetooth, plus an
  occasional catch-up check (default every 6 hours, configurable)
- **User management from HA** - create and delete scale user profiles with
  actions, no Wyze app needed
- **Auto-discovery** - HA detects the scale over Bluetooth automatically
- **Survives restarts** - all readings are stored in HA, so nothing goes
  unavailable between weigh-ins

---

## Devices and entities

### Wyze Scale (main device)

| Entity | Description |
|--------|-------------|
| Poll now | Button - connect and sync immediately |
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

> **Which user is which?** Users the Wyze app created are imported
> automatically and named like "Scale user A1B2C3". Weigh yourself once, see
> which device updated, then rename it (or edit its profile - see below).

---

## Managing scale users

Scale users are managed entirely from the UI, per scale. On the scale's
device page (**Settings → Devices & Services → Wyze Scale → the scale**):

- **Add user** - a button that opens a form (name, sex, age, height,
  approximate weight, athlete / weight-only options). The user is created on
  the scale and appears as a new sub-device.
- **Configure** on a user - edit that person's profile; changes are pushed
  to the scale on the next sync.
- **Delete** a user - removes it from the scale and deletes the sub-device.

Because each scale is its own device, there's no ambiguity when you have
more than one scale: you manage each scale's users from that scale's page.

Height and weight in the form follow your Home Assistant unit system:
feet/inches and pounds if you use US customary units, centimeters and
kilograms otherwise.

Users created in the Wyze app are imported automatically as editable users
the first time HA sees them. The approximate weight is only used by the
scale to match a weigh-in to the closest user, so it just needs to be
roughly right.

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

A discovered **Wyze Scale** appears under **Settings → Devices &
Services** - click **Configure** and confirm. If it isn't detected, step
on the scale to prompt an advertisement, then check again.

### Manual setup

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **Wyze Scale**
3. Pick your scale from the list (if it's empty, make sure the scale has
   power and is in range, then step on it and retry)

### Options

Click **Configure** on the integration entry to adjust:

| Option | Default | Description |
|--------|---------|-------------|
| Sync automatically when the scale wakes up | on | Connect when the scale (re)appears over Bluetooth |
| Minimum seconds between automatic syncs | 120 | Rate limit for automatic syncs - protects the scale's battery |
| Also sync every N seconds | 21600 (6 h) | Periodic catch-up sync in case the advertisement trigger missed one; 0 disables |
| Unit shown on the scale display | none | Push kg or lb to the scale's display (doesn't affect HA units) |

---

## How syncing works

The scale is battery powered, so the integration is built to be gentle on
it: it never holds a Bluetooth connection open and never polls on a fixed
schedule.

1. Home Assistant **listens passively** for the scale's Bluetooth
   advertisements - this costs the scale nothing.
2. When the scale appears (or reappears after being out of range), the
   integration **connects briefly**, collects any stored measurements and
   the live weigh-in, and **disconnects**.
3. As a safety net, a **periodic catch-up sync** (default every 6 hours,
   configurable, 0 to disable) collects anything the advertisement trigger
   missed. If the scale can't be reached, it just tries again next time.
4. Everything is saved in Home Assistant, so entities keep their values
   between weigh-ins and across HA restarts.

> **Note:** collected measurements are removed from the scale's internal
> memory as they're synced (this is how the scale's protocol works - the
> Wyze app does the same). After a sync, Home Assistant holds the only copy.

---

## Known limitations

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

The scale is momentarily unreachable over Bluetooth. Wait a moment and try
again (stepping on the scale will also wake it), or just let the automatic
sync handle it. If it happens often, move the Bluetooth adapter/proxy
closer to the scale.

### No devices found during setup

Confirm HA's Bluetooth integration works (Settings → Devices & Services →
Bluetooth) and the scale is powered and within range of the adapter/proxy.
Stepping on the scale prompts a fresh advertisement, which can help it be
discovered.

### A weigh-in didn't show up

- Check the **Last sync** sensor - if it's stale, HA hasn't synced
  recently; move the Bluetooth adapter/proxy closer to the scale.
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
