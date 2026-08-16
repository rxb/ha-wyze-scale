"""Coordinator for the Wyze Scale integration.

Battery-friendly design: the integration never keeps a connection open.
It listens passively for the scale's BLE advertisements (the scale only
powers its radio when in use) and connects briefly to sync time, read the
user list, and drain cached history records - then disconnects. A periodic
fallback sync (skipped silently while the scale is asleep) catches missed
wake-ups, and a manual "Poll now" button forces a session.

Because acknowledging a history record deletes it from the scale, all data
is persisted to HA storage as soon as a session ends, and restored on
startup.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from types import MappingProxyType

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ADDRESS,
    CONF_ADVERTISEMENT_TRIGGER,
    CONF_DISPLAY_UNIT,
    CONF_FALLBACK_INTERVAL,
    CONF_SYNC_COOLDOWN,
    DEFAULT_ADVERTISEMENT_TRIGGER,
    DEFAULT_FALLBACK_INTERVAL,
    DEFAULT_SYNC_COOLDOWN,
    DOMAIN,
    EVENT_MEASUREMENT,
    LIVE_FIRST_GRACE,
    LIVE_IDLE_TIMEOUT,
    LIVE_MAX_WAIT,
    STORAGE_VERSION,
    SUBENTRY_TYPE_USER,
    UNIT_OPTION_KG,
    UNIT_OPTION_LB,
    UNIT_OPTION_NONE,
)
from .users import UserProfile, fields_match, merge_import_name, reconcile
from .wyze_ble import (
    Measurement,
    UserRecord,
    WyzeScaleClient,
    WyzeScaleError,
)
from .wyze_ble.protocol import MEASURE_STATE_FINAL, UNIT_KG, UNIT_LB


def _profile_from_record(record: UserRecord) -> UserProfile:
    """Convert a scale user record into a UserProfile (no display name)."""
    return UserProfile(
        user_id=record.user_id_hex,
        name="",
        sex_male=bool(record.sex),
        age=record.age,
        height_cm=record.height,
        weight_kg=record.weight_raw / 100,
        athlete_mode=bool(record.athlete_mode),
        weight_only=bool(record.only_weight),
    )


def _record_from_profile(profile: UserProfile) -> UserRecord:
    """Convert a UserProfile into a 25-byte scale user record."""
    return UserRecord(
        user_id=bytes.fromhex(profile.user_id),
        weight_raw=round(profile.weight_kg * 100),
        sex=1 if profile.sex_male else 0,
        age=int(profile.age),
        height=int(profile.height_cm),
        athlete_mode=1 if profile.athlete_mode else 0,
        only_weight=1 if profile.weight_only else 0,
        last_impedance=0,
    )

_LOGGER = logging.getLogger(__name__)


def _utc_to_device_epoch(now_utc: datetime) -> int:
    """The scale's clock is standard Unix time (UTC), like the Wyze app.

    The app sends System.currentTimeMillis()/1000 with no timezone offset,
    so SYNC_TIME is just the real UTC epoch.
    """
    return int(now_utc.timestamp())


def _device_epoch_to_utc(ts: int) -> datetime:
    """History record timestamps are standard Unix time (UTC)."""
    return datetime.fromtimestamp(ts, timezone.utc)


@dataclass
class MeasurementData:
    """A scaled, timestamped measurement as exposed to entities."""

    time: datetime
    source: str  # "live" or "history"
    weight_kg: float
    bmi: float | None = None
    body_fat_pct: float | None = None
    muscle_mass_kg: float | None = None
    bone_mass_kg: float | None = None
    water_pct: float | None = None
    protein_pct: float | None = None
    lean_body_mass_kg: float | None = None
    visceral_fat_level: int | None = None
    bmr: int | None = None
    body_age: int | None = None
    impedance: int | None = None

    @classmethod
    def from_measurement(
        cls, m: Measurement, when: datetime, source: str
    ) -> "MeasurementData":
        return cls(
            time=when,
            source=source,
            weight_kg=m.weight_kg,
            bmi=m.bmi,
            body_fat_pct=m.body_fat_pct,
            muscle_mass_kg=m.muscle_mass_kg,
            bone_mass_kg=m.bone_mass_kg,
            water_pct=m.water_pct,
            protein_pct=m.protein_pct,
            lean_body_mass_kg=m.lean_body_mass_kg,
            visceral_fat_level=m.vfal or None,
            bmr=m.bmr or None,
            body_age=m.body_age or None,
            impedance=m.impedance or None,
        )

    def as_dict(self) -> dict[str, Any]:
        data = self.__dict__.copy()
        data["time"] = self.time.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MeasurementData":
        data = dict(data)
        data["time"] = dt_util.parse_datetime(data["time"]) or dt_util.utcnow()
        return cls(**data)


@dataclass
class UserData:
    """One scale user: profile plus latest measurement."""

    user_id: str  # hex
    sex: int = 0
    age: int = 0
    height: int = 0
    athlete_mode: int = 0
    only_weight: int = 0
    last: MeasurementData | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "sex": self.sex,
            "age": self.age,
            "height": self.height,
            "athlete_mode": self.athlete_mode,
            "only_weight": self.only_weight,
            "last": self.last.as_dict() if self.last else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UserData":
        last = data.get("last")
        return cls(
            user_id=data["user_id"],
            sex=data.get("sex", 0),
            age=data.get("age", 0),
            height=data.get("height", 0),
            athlete_mode=data.get("athlete_mode", 0),
            only_weight=data.get("only_weight", 0),
            last=MeasurementData.from_dict(last) if last else None,
        )


@dataclass
class ScaleData:
    """Aggregate integration state."""

    battery: int | None = None
    unit: int | None = None
    last_sync: datetime | None = None
    users: dict[str, UserData] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "battery": self.battery,
            "unit": self.unit,
            "last_sync": self.last_sync.isoformat() if self.last_sync else None,
            "users": {uid: user.as_dict() for uid, user in self.users.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScaleData":
        last_sync = data.get("last_sync")
        return cls(
            battery=data.get("battery"),
            unit=data.get("unit"),
            last_sync=dt_util.parse_datetime(last_sync) if last_sync else None,
            users={
                uid: UserData.from_dict(user)
                for uid, user in data.get("users", {}).items()
            },
        )


class WyzeScaleCoordinator(DataUpdateCoordinator[ScaleData]):
    """Advertisement-triggered sync coordinator."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        # Primarily event-driven (advertisement wake-up detection). The
        # periodic fallback guarantees cached history is eventually drained
        # even if the scale advertises continuously and no wake-up signal
        # can be derived. 0 disables it.
        fallback = entry.options.get(
            CONF_FALLBACK_INTERVAL, DEFAULT_FALLBACK_INTERVAL
        )
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}-{entry.data[CONF_ADDRESS]}",
            update_interval=timedelta(seconds=fallback) if fallback else None,
        )
        self._address: str = entry.data[CONF_ADDRESS]
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._scale_data = ScaleData()
        self._sync_lock = asyncio.Lock()
        self._last_sync_attempt: float = 0.0
        self._last_live: float | None = None
        self._cancel_bluetooth: Callable[[], None] | None = None
        self._manual_poll = False
        # User-management reconciliation state (persisted).
        self._tombstones: set[str] = set()  # user_ids deleted in HA
        self._pushed: dict[str, list[int]] = {}  # user_id -> scale_fields
        self._known_subentries: set[str] = set()  # last-seen subentry user_ids
        # Per-session scratch (reset each sync).
        self._deleted_this_session: set[str] = set()
        self._pending_imports: list[UserProfile] = []

    @property
    def address(self) -> str:
        return self._address

    def _subentry_profiles(self) -> dict[str, tuple[str, UserProfile]]:
        """Desired users from config subentries: user_id -> (subentry_id, profile)."""
        result: dict[str, tuple[str, UserProfile]] = {}
        for subentry_id, subentry in self.config_entry.subentries.items():
            if subentry.subentry_type != SUBENTRY_TYPE_USER:
                continue
            profile = UserProfile.from_subentry_data(dict(subentry.data))
            result[profile.user_id] = (subentry_id, profile)
        return result

    # ------------------------------------------------------------------
    # Setup / teardown
    # ------------------------------------------------------------------

    async def async_load(self) -> None:
        """Restore persisted state and start listening for advertisements."""
        stored = await self._store.async_load()
        if stored:
            # Back-compat: earlier versions stored ScaleData at the top level.
            scale_data = stored.get("scale_data", stored)
            self._scale_data = ScaleData.from_dict(scale_data)
            self._tombstones = set(stored.get("tombstones", []))
            self._pushed = {
                uid: list(fields)
                for uid, fields in stored.get("pushed_profiles", {}).items()
            }
            self._known_subentries = set(stored.get("known_subentries", []))
        self.async_set_updated_data(self._scale_data)

        # Detect users deleted in HA while we were unloaded: they were known
        # subentries before but are gone now. Tombstone them so the next sync
        # removes them from the scale (and doesn't re-import them).
        current = set(self._subentry_profiles())
        deleted = self._known_subentries - current
        if deleted:
            self._tombstones |= deleted
            for uid in deleted:
                self._pushed.pop(uid, None)
                self._scale_data.users.pop(uid, None)
        self._known_subentries = current
        await self._async_save()

        self._cancel_bluetooth = bluetooth.async_register_callback(
            self.hass,
            self._async_handle_advertisement,
            bluetooth.BluetoothCallbackMatcher(address=self._address),
            bluetooth.BluetoothScanningMode.PASSIVE,
        )

        # If there's pending user-management work (a delete, an added or
        # edited profile not yet on the scale), sync soon to push it.
        if self._has_pending_user_work():
            self.hass.async_create_task(self.async_request_refresh())

    def _has_pending_user_work(self) -> bool:
        if self._tombstones:
            return True
        for user_id, (_sid, profile) in self._subentry_profiles().items():
            if not fields_match(self._pushed.get(user_id, []), profile.scale_fields()):
                return True
        return False

    def diagnostics_data(self) -> dict[str, Any]:
        """Snapshot of persisted state for the diagnostics download."""
        return self._data_for_store()

    def _data_for_store(self) -> dict[str, Any]:
        return {
            "scale_data": self._scale_data.as_dict(),
            "tombstones": sorted(self._tombstones),
            "pushed_profiles": self._pushed,
            "known_subentries": sorted(self._known_subentries),
        }

    async def _async_save(self) -> None:
        await self._store.async_save(self._data_for_store())

    async def async_shutdown(self) -> None:
        if self._cancel_bluetooth is not None:
            self._cancel_bluetooth()
            self._cancel_bluetooth = None
        await super().async_shutdown()

    # ------------------------------------------------------------------
    # Triggers
    # ------------------------------------------------------------------

    @callback
    def _async_handle_advertisement(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        _change: bluetooth.BluetoothChange,
    ) -> None:
        """Sync when HA reports scale (re)appearance or advertisement change.

        HA's bluetooth manager already deduplicates advertisements: this
        callback fires only when the scale newly appears (including after
        an absence - i.e. it woke up), or when its advertisement content
        changes. Either is a wake-up/activity signal, so any callback is a
        sync trigger, rate-limited by the configured cooldown to bound how
        often we power up the scale's radio with a connection.
        """
        if not self.config_entry.options.get(
            CONF_ADVERTISEMENT_TRIGGER, DEFAULT_ADVERTISEMENT_TRIGGER
        ):
            return
        if self._sync_lock.locked():
            return
        cooldown = self.config_entry.options.get(
            CONF_SYNC_COOLDOWN, DEFAULT_SYNC_COOLDOWN
        )
        now = time.monotonic()
        if self._last_sync_attempt and now - self._last_sync_attempt < cooldown:
            return
        self._last_sync_attempt = now
        _LOGGER.debug(
            "Advertisement event from %s (RSSI %s); starting sync",
            service_info.address,
            service_info.rssi,
        )
        self.hass.async_create_task(self.async_request_refresh())

    async def async_poll_now(self) -> None:
        """User-requested sync (Poll now button); bypasses the cooldown."""
        self._last_sync_attempt = time.monotonic()
        self._manual_poll = True
        try:
            await self.async_refresh()
        finally:
            self._manual_poll = False
        if not self.last_update_success and self.last_exception is not None:
            raise self.last_exception

    # ------------------------------------------------------------------
    # Sync session
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> ScaleData:
        async with self._sync_lock:
            self._last_sync_attempt = time.monotonic()
            return await self._async_sync_session()

    async def _async_sync_session(self) -> ScaleData:
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self._address, connectable=True
        )
        if ble_device is None:
            if self._manual_poll:
                raise UpdateFailed(
                    translation_domain=DOMAIN,
                    translation_key="scale_not_reachable",
                )
            # Scheduled fallback / advertisement race: the scale is simply
            # asleep. Not an error worth flapping entities or logs over.
            _LOGGER.debug("Fallback sync skipped: scale not advertising")
            return self._scale_data

        self._last_live = None
        self._deleted_this_session = set()
        self._pending_imports = []
        client = WyzeScaleClient(on_live_weight=self._handle_live_weight)
        try:
            await client.connect(ble_device)
            await client.sync_time(_utc_to_device_epoch(dt_util.utcnow()))

            unit_option = self.config_entry.options.get(
                CONF_DISPLAY_UNIT, UNIT_OPTION_NONE
            )
            if unit_option in (UNIT_OPTION_KG, UNIT_OPTION_LB):
                try:
                    await client.set_unit(
                        UNIT_KG if unit_option == UNIT_OPTION_KG else UNIT_LB
                    )
                except WyzeScaleError as err:
                    _LOGGER.warning("Failed to set display unit: %s", err)

            users = await client.get_users()
            for record in users:
                self._merge_profile(record)

            # Push HA-side user changes and import scale-side users.
            try:
                await self._async_reconcile_users(client, users)
            except WyzeScaleError as err:
                _LOGGER.warning("User reconciliation aborted: %s", err)

            for record in users:
                # Skip users we just deleted from the scale this session.
                if record.user_id_hex in self._deleted_this_session:
                    continue
                try:
                    # Records are recorded (and queued for disk) via the
                    # callback BEFORE each ack deletes them from the scale.
                    history = await client.drain_history(
                        record,
                        on_record=lambda m: self._record_measurement(
                            m, source="history"
                        ),
                    )
                except WyzeScaleError as err:
                    # One user's drain failing shouldn't strand the rest.
                    _LOGGER.warning(
                        "History sync for user %s… failed: %s",
                        record.user_id_hex[:8],
                        err,
                    )
                    continue
                if history:
                    _LOGGER.debug(
                        "Synced %d history record(s) for user %s…",
                        len(history),
                        record.user_id_hex[:8],
                    )

            await self._async_linger_for_live(client)
            self._scale_data.last_sync = dt_util.utcnow()
            return self._scale_data
        except Exception as err:
            # Anything drained before the failure is already recorded and
            # queued for persistence; only the session status is a failure.
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="sync_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        finally:
            await client.disconnect()
            # Don't clobber a freshly-reloaded coordinator's store: if we're
            # being torn down (e.g. an import or options change triggered a
            # reload), the new coordinator owns the state now.
            if not self._shutdown_requested:
                await self._async_save()
                self._apply_pending_imports()

    async def _async_linger_for_live(self, client: WyzeScaleClient) -> None:
        """Stay connected while live weight frames are streaming."""
        start = time.monotonic()
        while client.is_connected:
            now = time.monotonic()
            if self._last_live is None:
                # Give someone stepping on just after the wake-up a moment
                # to produce the first live frame.
                if now - start >= LIVE_FIRST_GRACE:
                    return
            elif now - self._last_live >= LIVE_IDLE_TIMEOUT:
                return
            elif now - start >= LIVE_MAX_WAIT:
                _LOGGER.debug("Live-data linger hit max wait; disconnecting")
                return
            await asyncio.sleep(1)

    # ------------------------------------------------------------------
    # User reconciliation (config subentries <-> scale)
    # ------------------------------------------------------------------

    async def _async_reconcile_users(
        self, client: WyzeScaleClient, records: list[UserRecord]
    ) -> None:
        """Make the scale's users agree with the HA config subentries.

        Runs inside a sync session with an open connection. Pushes HA-side
        creates/updates/deletes to the scale, and stages scale-side users
        (e.g. created in the Wyze app) for import as new subentries. The
        actual subentry creation is deferred to _apply_pending_imports (run
        after the session's store save) to avoid a reload-vs-save race.
        """
        scale_users = {r.user_id_hex: _profile_from_record(r) for r in records}
        subentries = {
            uid: profile for uid, (_sid, profile) in self._subentry_profiles().items()
        }
        plan = reconcile(scale_users, subentries, self._tombstones)
        if plan.is_empty and not plan.tombstones_cleared:
            self._known_subentries = set(subentries)
            return

        for user_id in plan.to_delete:
            await client.delete_user(bytes.fromhex(user_id))
            self._deleted_this_session.add(user_id)
            self._tombstones.discard(user_id)
            self._pushed.pop(user_id, None)
            self._scale_data.users.pop(user_id, None)
            _LOGGER.info("Deleted scale user %s from the scale", user_id[:8])

        for user_id in plan.tombstones_cleared:
            self._tombstones.discard(user_id)

        for profile in plan.to_create:
            record = _record_from_profile(profile)
            await client.select_user(record)  # create flow: select then store
            await client.update_user(record)
            self._pushed[profile.user_id] = list(profile.scale_fields())
            self._merge_profile(record)
            _LOGGER.info("Created scale user %s on the scale", profile.user_id[:8])

        for profile in plan.to_update:
            record = _record_from_profile(profile)
            await client.update_user(record)
            self._pushed[profile.user_id] = list(profile.scale_fields())
            self._merge_profile(record)
            _LOGGER.info("Updated scale user %s on the scale", profile.user_id[:8])

        for profile in plan.to_import:
            named = merge_import_name(profile)
            self._pending_imports.append(named)
            # Already on the scale, so record it as pushed now so the store
            # save reflects it before the import triggers a reload.
            self._pushed[named.user_id] = list(named.scale_fields())

        # Imports are NOT marked known here: adding a subentry starts the
        # entry reload eagerly, so a coordinator can load while later
        # imports in _apply_pending_imports aren't subentries yet - and a
        # pre-marked import would look like a user deleted while unloaded,
        # get tombstoned, and be wrongly deleted from the scale.
        self._known_subentries = set(subentries)

    def _apply_pending_imports(self) -> None:
        """Create HA subentries for users discovered on the scale.

        Called after the session's store save, so a reloaded coordinator
        reads current data. Each async_add_subentry fires the entry update
        listener, and with eager task execution the resulting reload can
        start (and a new coordinator can load) before the NEXT iteration
        of this loop - which is why imports only become "known" here, one
        by one, once their subentry actually exists.
        """
        for profile in self._pending_imports:
            subentry = ConfigSubentry(
                data=MappingProxyType(profile.to_subentry_data()),
                subentry_type=SUBENTRY_TYPE_USER,
                title=profile.name,
                unique_id=profile.user_id,
            )
            try:
                self.hass.config_entries.async_add_subentry(
                    self.config_entry, subentry
                )
            except Exception as err:  # noqa: BLE001 - e.g. AbortFlow on dup id
                _LOGGER.warning(
                    "Could not import scale user %s: %s", profile.user_id[:8], err
                )
                continue
            self._known_subentries.add(profile.user_id)
            _LOGGER.info("Imported scale user %s as a device", profile.user_id[:8])
        self._pending_imports = []

    # ------------------------------------------------------------------
    # Data handling
    # ------------------------------------------------------------------

    def _handle_live_weight(self, m: Measurement) -> None:
        self._last_live = time.monotonic()
        if m.battery is not None:
            self._scale_data.battery = m.battery
        if m.unit is not None:
            self._scale_data.unit = m.unit
        if m.measure_state != MEASURE_STATE_FINAL or not m.weight_raw:
            return
        # The scale repeats the settled frame while the person stands on
        # it; record each weigh-in only once.
        user = self._scale_data.users.get(m.user_id_hex)
        if (
            user is not None
            and user.last is not None
            and user.last.source == "live"
            and user.last.weight_kg == m.weight_kg
            and (dt_util.utcnow() - user.last.time).total_seconds() < 120
        ):
            return
        _LOGGER.debug(
            "Final live measurement: %.2f kg (user %s…)",
            m.weight_kg,
            m.user_id_hex[:8],
        )
        self._record_measurement(m, source="live")

    def _merge_profile(self, record: UserRecord) -> UserData:
        user = self._scale_data.users.get(record.user_id_hex)
        if user is None:
            user = UserData(user_id=record.user_id_hex)
            self._scale_data.users[record.user_id_hex] = user
        user.sex = record.sex
        user.age = record.age
        user.height = record.height
        user.athlete_mode = record.athlete_mode
        user.only_weight = record.only_weight
        return user

    def _record_measurement(self, m: Measurement, source: str) -> None:
        """Record one weigh-in: fire the event, update state, queue a save.

        Called once per measurement (history records are deleted from the
        scale right after this runs), so every path here must be durable:
        the event is the permanent record, the entity state holds the
        newest reading, and the store write survives restarts.
        """
        if not m.user_id.strip(b"\x00"):
            _LOGGER.debug("Measurement with empty user_id; skipping")
            return
        when = (
            _device_epoch_to_utc(m.timestamp)
            if m.timestamp
            else dt_util.utcnow()
        )
        user = self._scale_data.users.get(m.user_id_hex)
        if user is None:
            user = UserData(user_id=m.user_id_hex)
            self._scale_data.users[m.user_id_hex] = user
        user.sex = m.sex
        user.age = m.age
        user.height = m.height
        user.athlete_mode = m.athlete_mode
        user.only_weight = m.only_weight
        data = MeasurementData.from_measurement(m, when, source)
        self.hass.bus.async_fire(
            EVENT_MEASUREMENT,
            {
                "device_address": self._address,
                "user_id": m.user_id_hex,
                **data.as_dict(),
            },
        )
        if user.last is None or when >= user.last.time:
            user.last = data
        self.async_update_listeners()
        self._store.async_delay_save(self._data_for_store, 2)
