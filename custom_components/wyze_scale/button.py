"""Poll-now button for the Wyze Scale."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import WyzeScaleConfigEntry
from .const import DOMAIN
from .coordinator import WyzeScaleCoordinator
from .sensor import scale_device_info

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WyzeScaleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([WyzeScalePollNowButton(entry.runtime_data)])


class WyzeScalePollNowButton(CoordinatorEntity[WyzeScaleCoordinator], ButtonEntity):
    """Force an immediate connect-and-sync session.

    Note: the scale must be awake (someone recently on it, or advertising)
    for this to succeed; it cannot wake a sleeping scale.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "poll_now"

    def __init__(self, coordinator: WyzeScaleCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}-poll_now"
        self._attr_device_info = scale_device_info(coordinator)

    @property
    def available(self) -> bool:
        return True

    async def async_press(self) -> None:
        try:
            await self.coordinator.async_poll_now()
        except HomeAssistantError:
            # Coordinator failures already carry translated messages.
            raise
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="sync_failed",
                translation_placeholders={"error": str(err)},
            ) from err
