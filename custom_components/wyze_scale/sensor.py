"""Sensors for the Wyze Scale: scale-level plus one sub-device per user."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfMass,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import WyzeScaleConfigEntry
from .const import DOMAIN, SUBENTRY_TYPE_USER
from .coordinator import ScaleData, UserData, WyzeScaleCoordinator
from .users import UserProfile


@dataclass(frozen=True, kw_only=True)
class WyzeScaleSensorDescription(SensorEntityDescription):
    """Scale-level sensor description."""

    value_fn: Callable[[ScaleData], Any]


@dataclass(frozen=True, kw_only=True)
class WyzeScaleUserSensorDescription(SensorEntityDescription):
    """Per-user sensor description."""

    value_fn: Callable[[UserData], Any]


SCALE_SENSORS: tuple[WyzeScaleSensorDescription, ...] = (
    WyzeScaleSensorDescription(
        key="battery",
        name="Battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.battery,
    ),
    WyzeScaleSensorDescription(
        key="last_sync",
        name="Last sync",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.last_sync,
    ),
)


def _measurement(
    attr: str,
) -> Callable[[UserData], Any]:
    def getter(user: UserData) -> Any:
        if user.last is None:
            return None
        return getattr(user.last, attr)

    return getter


USER_SENSORS: tuple[WyzeScaleUserSensorDescription, ...] = (
    WyzeScaleUserSensorDescription(
        key="weight",
        name="Weight",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=_measurement("weight_kg"),
    ),
    WyzeScaleUserSensorDescription(
        key="bmi",
        name="BMI",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:human",
        value_fn=_measurement("bmi"),
    ),
    WyzeScaleUserSensorDescription(
        key="body_fat",
        name="Body fat",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:percent",
        value_fn=_measurement("body_fat_pct"),
    ),
    WyzeScaleUserSensorDescription(
        key="muscle_mass",
        name="Muscle mass",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:arm-flex",
        value_fn=_measurement("muscle_mass_kg"),
    ),
    WyzeScaleUserSensorDescription(
        key="bone_mass",
        name="Bone mass",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:bone",
        value_fn=_measurement("bone_mass_kg"),
    ),
    WyzeScaleUserSensorDescription(
        key="body_water",
        name="Body water",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:water-percent",
        value_fn=_measurement("water_pct"),
    ),
    WyzeScaleUserSensorDescription(
        key="protein",
        name="Protein",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:food-drumstick",
        value_fn=_measurement("protein_pct"),
    ),
    WyzeScaleUserSensorDescription(
        key="lean_body_mass",
        name="Lean body mass",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=_measurement("lean_body_mass_kg"),
    ),
    WyzeScaleUserSensorDescription(
        key="visceral_fat",
        name="Visceral fat level",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:stomach",
        value_fn=_measurement("visceral_fat_level"),
    ),
    WyzeScaleUserSensorDescription(
        key="bmr",
        name="Basal metabolic rate",
        native_unit_of_measurement="kcal",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:fire",
        value_fn=_measurement("bmr"),
    ),
    WyzeScaleUserSensorDescription(
        key="body_age",
        name="Metabolic age",
        icon="mdi:calendar-account",
        value_fn=_measurement("body_age"),
    ),
    WyzeScaleUserSensorDescription(
        key="impedance",
        name="Impedance",
        native_unit_of_measurement="Ω",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:omega",
        value_fn=_measurement("impedance"),
    ),
    WyzeScaleUserSensorDescription(
        key="last_measurement",
        name="Last measurement",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_measurement("time"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WyzeScaleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up scale-level sensors and one sensor set per user subentry.

    Each scale user is a config subentry; its sensors live on a sub-device
    tied to that subentry. New/edited/removed subentries are picked up when
    the entry reloads (the coordinator schedules a reload after importing a
    user, and HA reloads after any subentry change).
    """
    coordinator = entry.runtime_data

    async_add_entities(
        WyzeScaleSensor(coordinator, description) for description in SCALE_SENSORS
    )

    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != SUBENTRY_TYPE_USER:
            continue
        async_add_entities(
            [
                WyzeScaleUserSensor(coordinator, subentry, description)
                for description in USER_SENSORS
            ],
            config_subentry_id=subentry_id,
        )


def scale_device_info(coordinator: WyzeScaleCoordinator) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.address)},
        connections={(CONNECTION_BLUETOOTH, coordinator.address)},
        manufacturer="Wyze",
        model="Scale X",
        name="Wyze Scale",
    )


class WyzeScaleBaseEntity(CoordinatorEntity[WyzeScaleCoordinator]):
    """Base entity: always available, state comes from persisted data."""

    _attr_has_entity_name = True

    @property
    def available(self) -> bool:
        # The scale is asleep and unreachable most of the time; entities
        # expose the last persisted measurement rather than going
        # unavailable between syncs.
        return True


class WyzeScaleSensor(WyzeScaleBaseEntity, SensorEntity):
    """Scale-level sensor."""

    entity_description: WyzeScaleSensorDescription

    def __init__(
        self,
        coordinator: WyzeScaleCoordinator,
        description: WyzeScaleSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.address}-{description.key}"
        self._attr_device_info = scale_device_info(coordinator)

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)


class WyzeScaleUserSensor(WyzeScaleBaseEntity, SensorEntity):
    """Per-user sensor, attached to a user sub-device (config subentry).

    Measurement values come from the coordinator (keyed by user_id); the
    profile shown as attributes comes from the subentry the user edits.
    """

    entity_description: WyzeScaleUserSensorDescription

    def __init__(
        self,
        coordinator: WyzeScaleCoordinator,
        subentry: ConfigSubentry,
        description: WyzeScaleUserSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._subentry = subentry
        self._user_id = subentry.data["user_id"]
        self._attr_unique_id = f"{coordinator.address}-{self._user_id}-{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.address}-{self._user_id}")},
            via_device=(DOMAIN, coordinator.address),
            manufacturer="Wyze",
            model="Scale X user",
            name=subentry.title,
        )

    @property
    def native_value(self) -> Any:
        user = self.coordinator.data.users.get(self._user_id)
        if user is None:
            return None
        return self.entity_description.value_fn(user)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.key != "weight":
            return None
        profile = UserProfile.from_subentry_data(dict(self._subentry.data))
        user = self.coordinator.data.users.get(self._user_id)
        return {
            "user_id": self._user_id,
            "sex": "male" if profile.sex_male else "female",
            "age": profile.age,
            "height_cm": profile.height_cm,
            "athlete_mode": profile.athlete_mode,
            "weight_only_mode": profile.weight_only,
            "measurement_source": user.last.source if user and user.last else None,
        }
