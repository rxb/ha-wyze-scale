"""Constants for the Wyze Scale integration."""

from __future__ import annotations

DOMAIN = "wyze_scale"

CONF_ADDRESS = "address"

# Options
CONF_ADVERTISEMENT_TRIGGER = "advertisement_trigger"
CONF_SYNC_COOLDOWN = "sync_cooldown"
CONF_DISPLAY_UNIT = "display_unit"

DEFAULT_ADVERTISEMENT_TRIGGER = True
# Minimum seconds between advertisement-triggered syncs. Syncs are already
# gated on wake-up events (observed behavior: the scale advertises ~every
# 2 s for ~5 min after being disturbed, then goes silent), so this is a
# backstop against pathological re-trigger loops, not the primary limiter.
DEFAULT_SYNC_COOLDOWN = 120
MIN_SYNC_COOLDOWN = 30
MAX_SYNC_COOLDOWN = 86400

# Periodic fallback sync (seconds); guarantees cached history is drained
# even if no wake-up can be detected from advertisements. 0 = disabled.
CONF_FALLBACK_INTERVAL = "fallback_interval"
DEFAULT_FALLBACK_INTERVAL = 21600  # 6 h
MAX_FALLBACK_INTERVAL = 604800

UNIT_OPTION_NONE = "none"
UNIT_OPTION_KG = "kg"
UNIT_OPTION_LB = "lb"

STORAGE_VERSION = 1

# After the last live-weight frame, wait this long before disconnecting in
# case the person is still on the scale.
LIVE_IDLE_TIMEOUT = 8.0
# If no live frame has arrived yet, wait this long for one before
# disconnecting (someone may be stepping on just after the wake-up).
LIVE_FIRST_GRACE = 5.0
# Hard cap on how long a session lingers waiting for live data.
LIVE_MAX_WAIT = 90.0

# Fired once per synced measurement (live and history) with the full
# scaled reading - the durable record of every weigh-in, since history
# records are deleted from the scale once acknowledged.
EVENT_MEASUREMENT = f"{DOMAIN}_measurement"
