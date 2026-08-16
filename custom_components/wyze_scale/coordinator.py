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
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
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
    UNIT_OPTION_KG,
    UNIT_OPTION_LB,
    UNIT_OPTION_NONE,
)
from .wyze_ble import (
    Measurement,
    UserRecord,
    WyzeScaleClient,
    WyzeScaleError,
)
from .wyze_ble.protocol import MEASURE_STATE_FINAL, UNIT_KG, UNIT_LB

_LOGGER = logging.getLogger(__name__)


def _utc_to_device_epoch(now_utc: datetime) -> int:
    """The scale's clock is local wall time encoded as epoch seconds."""
    local = dt_util.as_local(now_utc)
    return int(local.replace(tzinfo=timezone.utc).timestamp())


def _device_epoch_to_utc(ts: int) -> datetime:
    """Inverse of _utc_to_device_epoch for history record timestamps."""
    naive = datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)
    local_tz = dt_util.now().tzinfo
    return dt_util.as_utc(naive.replace(tzinfo=local_tz))


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

    @property
    def address(self) -> str:
        return self._address

    # ------------------------------------------------------------------
    # Setup / teardown
    # ------------------------------------------------------------------

    async def async_load(self) -> None:
        """Restore persisted state and start listening for advertisements."""
        stored = await self._store.async_load()
        if stored:
            self._scale_data = ScaleData.from_dict(stored)
        self.async_set_updated_data(self._scale_data)
        self._cancel_bluetooth = bluetooth.async_register_callback(
            self.hass,
            self._async_handle_advertisement,
            bluetooth.BluetoothCallbackMatcher(address=self._address),
            bluetooth.BluetoothScanningMode.PASSIVE,
        )

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
                    "Scale is not reachable - it sleeps when idle; step on "
                    "it to wake it and try again"
                )
            # Scheduled fallback / advertisement race: the scale is simply
            # asleep. Not an error worth flapping entities or logs over.
            _LOGGER.debug("Fallback sync skipped: scale not advertising")
            return self._scale_data

        self._last_live = None
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

            for record in users:
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
                    _LOGGER.warning(
                        "History sync for user %s… aborted: %s",
                        record.user_id_hex[:8],
                        err,
                    )
                    break
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
            raise UpdateFailed(f"Sync with scale failed: {err}") from err
        finally:
            await client.disconnect()
            await self._store.async_save(self._scale_data.as_dict())

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
    # User management (add_user / delete_user services)
    # ------------------------------------------------------------------

    async def _async_connect_for_command(self) -> WyzeScaleClient:
        """Open a short command session; caller must disconnect."""
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self._address, connectable=True
        )
        if ble_device is None:
            raise HomeAssistantError(
                "Scale is not reachable - it sleeps when idle; step on it "
                "to wake it and try again"
            )
        client = WyzeScaleClient(on_live_weight=self._handle_live_weight)
        try:
            await client.connect(ble_device)
            await client.sync_time(_utc_to_device_epoch(dt_util.utcnow()))
        except Exception as err:
            await client.disconnect()
            raise HomeAssistantError(f"Failed to connect to scale: {err}") from err
        return client

    async def async_add_user(
        self,
        *,
        sex_male: bool,
        age: int,
        height_cm: int,
        weight_kg: float,
        athlete_mode: bool = False,
        weight_only: bool = False,
    ) -> str:
        """Create a new user profile on the scale; returns its user_id hex.

        The weight is the person's approximate weight - the scale uses it
        to match weigh-ins to users, so it should be roughly right.
        """
        record = UserRecord(
            user_id=secrets.token_bytes(16),
            weight_raw=round(weight_kg * 100),
            sex=1 if sex_male else 0,
            age=age,
            height=height_cm,
            athlete_mode=1 if athlete_mode else 0,
            only_weight=1 if weight_only else 0,
            last_impedance=0,
        )
        async with self._sync_lock:
            client = await self._async_connect_for_command()
            try:
                # Create flow per PROTOCOL.md §5.9: select the record as
                # current user, then store it.
                await client.select_user(record)
                await client.update_user(record)
                for stored in await client.get_users():
                    self._merge_profile(stored)
                # get_users can miss the new record if the scale splits or
                # delays the reply; make sure it exists locally regardless.
                if record.user_id_hex not in self._scale_data.users:
                    self._merge_profile(record)
            except HomeAssistantError:
                raise
            except Exception as err:
                raise HomeAssistantError(f"Adding user failed: {err}") from err
            finally:
                await client.disconnect()
                await self._store.async_save(self._scale_data.as_dict())
                self.async_update_listeners()
        _LOGGER.info("Created scale user %s", record.user_id_hex)
        return record.user_id_hex

    async def async_delete_user(self, user_id_hex: str) -> None:
        """Delete a user profile from the scale and remove its sub-device."""
        try:
            user_id = bytes.fromhex(user_id_hex)
        except ValueError as err:
            raise HomeAssistantError(f"Invalid user_id: {err}") from err
        if len(user_id) != 16:
            raise HomeAssistantError("user_id must be 32 hex characters (16 bytes)")
        async with self._sync_lock:
            client = await self._async_connect_for_command()
            try:
                await client.delete_user(user_id)
            except Exception as err:
                raise HomeAssistantError(f"Deleting user failed: {err}") from err
            finally:
                await client.disconnect()
            self._scale_data.users.pop(user_id_hex, None)
            await self._store.async_save(self._scale_data.as_dict())
            self.async_update_listeners()
        device_registry = dr.async_get(self.hass)
        device = device_registry.async_get_device(
            identifiers={(DOMAIN, f"{self._address}-{user_id_hex}")}
        )
        if device is not None:
            device_registry.async_remove_device(device.id)
        _LOGGER.info("Deleted scale user %s", user_id_hex)

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
        self._store.async_delay_save(self._scale_data.as_dict, 2)
