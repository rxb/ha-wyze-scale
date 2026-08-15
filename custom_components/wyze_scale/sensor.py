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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import WyzeScaleConfigEntry
from .const import DOMAIN
from .coordinator import ScaleData, UserData, WyzeScaleCoordinator


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
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors; add per-user entities as users are discovered."""
    coordinator = entry.runtime_data

    async_add_entities(
        WyzeScaleSensor(coordinator, description) for description in SCALE_SENSORS
    )

    known_users: set[str] = set()

    @callback
    def _async_add_new_users() -> None:
        new_entities: list[SensorEntity] = []
        for user_id in coordinator.data.users:
            if user_id in known_users:
                continue
            known_users.add(user_id)
            new_entities.extend(
                WyzeScaleUserSensor(coordinator, user_id, description)
                for description in USER_SENSORS
            )
        if new_entities:
            async_add_entities(new_entities)

    _async_add_new_users()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_users))


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
    """Per-user sensor, attached to a user sub-device."""

    entity_description: WyzeScaleUserSensorDescription

    def __init__(
        self,
        coordinator: WyzeScaleCoordinator,
        user_id: str,
        description: WyzeScaleUserSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._user_id = user_id
        self._attr_unique_id = f"{coordinator.address}-{user_id}-{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.address}-{user_id}")},
            via_device=(DOMAIN, coordinator.address),
            manufacturer="Wyze",
            model="Scale X user",
            name=f"Scale user {user_id[:6].upper()}",
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
        user = self.coordinator.data.users.get(self._user_id)
        if user is None:
            return None
        return {
            "user_id": user.user_id,
            "sex": "male" if user.sex else "female",
            "age": user.age,
            "height_cm": user.height,
            "athlete_mode": bool(user.athlete_mode),
            "weight_only_mode": bool(user.only_weight),
            "measurement_source": user.last.source if user.last else None,
        }
