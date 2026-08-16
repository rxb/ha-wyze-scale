"""Config flow for the Wyze Scale integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import format_mac
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .const import (
    CONF_ADDRESS,
    CONF_ADVERTISEMENT_TRIGGER,
    CONF_AGE,
    CONF_ATHLETE_MODE,
    CONF_DISPLAY_UNIT,
    CONF_FALLBACK_INTERVAL,
    CONF_HEIGHT_CM,
    CONF_HEIGHT_FT,
    CONF_HEIGHT_IN,
    CONF_NAME,
    CONF_SEX,
    CONF_SYNC_COOLDOWN,
    CONF_WEIGHT_KG,
    CONF_WEIGHT_LB,
    CONF_WEIGHT_ONLY,
    DEFAULT_ADVERTISEMENT_TRIGGER,
    DEFAULT_FALLBACK_INTERVAL,
    DEFAULT_SYNC_COOLDOWN,
    DOMAIN,
    MAX_FALLBACK_INTERVAL,
    MAX_SYNC_COOLDOWN,
    MIN_SYNC_COOLDOWN,
    SEX_FEMALE,
    SEX_MALE,
    SUBENTRY_TYPE_USER,
    UNIT_OPTION_KG,
    UNIT_OPTION_LB,
    UNIT_OPTION_NONE,
)
from .users import (
    UserProfile,
    cm_to_ft_in,
    ft_in_to_cm,
    kg_to_lb,
    lb_to_kg,
)
from .wyze_ble import LOCAL_NAME, SERVICE_UUID


def _use_us_units(hass: HomeAssistant) -> bool:
    """Whether to present the user form in US-customary units."""
    return hass.config.units is US_CUSTOMARY_SYSTEM


def _form_defaults(profile: UserProfile | None, us: bool) -> dict[str, Any]:
    """Form field defaults for the given unit system from a canonical profile."""
    if profile is None:
        if us:
            return {CONF_HEIGHT_FT: 5, CONF_HEIGHT_IN: 8, CONF_WEIGHT_LB: 150}
        return {CONF_HEIGHT_CM: 170, CONF_WEIGHT_KG: 70}
    defaults: dict[str, Any] = {
        CONF_NAME: profile.name,
        CONF_SEX: SEX_MALE if profile.sex_male else SEX_FEMALE,
        CONF_AGE: profile.age,
        CONF_ATHLETE_MODE: profile.athlete_mode,
        CONF_WEIGHT_ONLY: profile.weight_only,
    }
    if us:
        feet, inches = cm_to_ft_in(profile.height_cm)
        defaults[CONF_HEIGHT_FT] = feet
        defaults[CONF_HEIGHT_IN] = inches
        defaults[CONF_WEIGHT_LB] = round(kg_to_lb(profile.weight_kg), 1)
    else:
        defaults[CONF_HEIGHT_CM] = profile.height_cm
        defaults[CONF_WEIGHT_KG] = profile.weight_kg
    return defaults


def _user_profile_schema(defaults: dict[str, Any], us: bool) -> vol.Schema:
    """Form schema for a scale user's biometric profile.

    Height/weight fields are shown in the user's unit system (ft+in / lb for
    US-customary, cm / kg otherwise) and converted to canonical cm/kg on save.
    """
    schema: dict[Any, Any] = {
        vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "")): TextSelector(),
        vol.Required(
            CONF_SEX, default=defaults.get(CONF_SEX, SEX_FEMALE)
        ): SelectSelector(
            SelectSelectorConfig(
                options=[SEX_MALE, SEX_FEMALE],
                translation_key="sex",
                mode=SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Required(CONF_AGE, default=defaults.get(CONF_AGE, 30)): NumberSelector(
            NumberSelectorConfig(min=1, max=120, step=1, mode=NumberSelectorMode.BOX)
        ),
    }
    if us:
        schema[
            vol.Required(CONF_HEIGHT_FT, default=defaults.get(CONF_HEIGHT_FT, 5))
        ] = NumberSelector(
            NumberSelectorConfig(
                min=1, max=8, step=1, unit_of_measurement="ft",
                mode=NumberSelectorMode.BOX,
            )
        )
        schema[
            vol.Required(CONF_HEIGHT_IN, default=defaults.get(CONF_HEIGHT_IN, 8))
        ] = NumberSelector(
            NumberSelectorConfig(
                min=0, max=11, step=1, unit_of_measurement="in",
                mode=NumberSelectorMode.BOX,
            )
        )
        schema[
            vol.Required(CONF_WEIGHT_LB, default=defaults.get(CONF_WEIGHT_LB, 150))
        ] = NumberSelector(
            NumberSelectorConfig(
                min=2, max=660, step=0.5, unit_of_measurement="lb",
                mode=NumberSelectorMode.BOX,
            )
        )
    else:
        schema[
            vol.Required(CONF_HEIGHT_CM, default=defaults.get(CONF_HEIGHT_CM, 170))
        ] = NumberSelector(
            NumberSelectorConfig(
                min=50, max=250, step=1, unit_of_measurement="cm",
                mode=NumberSelectorMode.BOX,
            )
        )
        schema[
            vol.Required(CONF_WEIGHT_KG, default=defaults.get(CONF_WEIGHT_KG, 70))
        ] = NumberSelector(
            NumberSelectorConfig(
                min=1, max=300, step=0.5, unit_of_measurement="kg",
                mode=NumberSelectorMode.BOX,
            )
        )
    schema[
        vol.Required(CONF_ATHLETE_MODE, default=defaults.get(CONF_ATHLETE_MODE, False))
    ] = BooleanSelector()
    schema[
        vol.Required(CONF_WEIGHT_ONLY, default=defaults.get(CONF_WEIGHT_ONLY, False))
    ] = BooleanSelector()
    return vol.Schema(schema)


def _profile_from_input(user_id: str, data: dict[str, Any], us: bool) -> UserProfile:
    if us:
        height_cm = ft_in_to_cm(int(data[CONF_HEIGHT_FT]), int(data[CONF_HEIGHT_IN]))
        weight_kg = lb_to_kg(float(data[CONF_WEIGHT_LB]))
    else:
        height_cm = int(data[CONF_HEIGHT_CM])
        weight_kg = float(data[CONF_WEIGHT_KG])
    return UserProfile(
        user_id=user_id,
        name=data[CONF_NAME],
        sex_male=data[CONF_SEX] == SEX_MALE,
        age=int(data[CONF_AGE]),
        height_cm=height_cm,
        weight_kg=weight_kg,
        athlete_mode=bool(data[CONF_ATHLETE_MODE]),
        weight_only=bool(data[CONF_WEIGHT_ONLY]),
    )


class WyzeScaleUserSubentryFlow(ConfigSubentryFlow):
    """Add or edit a scale user (one config subentry per user)."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add a new scale user."""
        us = _use_us_units(self.hass)
        if user_input is not None:
            profile = _profile_from_input(
                UserProfile.new_user_id(), user_input, us
            )
            return self.async_create_entry(
                title=profile.name,
                data=profile.to_subentry_data(),
                unique_id=profile.user_id,
            )
        return self.async_show_form(
            step_id="user", data_schema=_user_profile_schema(_form_defaults(None, us), us)
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit an existing scale user's profile."""
        subentry = self._get_reconfigure_subentry()
        existing = UserProfile.from_subentry_data(dict(subentry.data))
        us = _use_us_units(self.hass)
        if user_input is not None:
            profile = _profile_from_input(existing.user_id, user_input, us)
            return self.async_update_and_abort(
                self._get_entry(),
                subentry,
                title=profile.name,
                data=profile.to_subentry_data(),
            )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_user_profile_schema(_form_defaults(existing, us), us),
        )


def _is_wyze_scale(service_info: BluetoothServiceInfoBleak) -> bool:
    return (
        SERVICE_UUID in service_info.service_uuids
        or service_info.name == LOCAL_NAME
    )


class WyzeScaleConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle discovery and manual setup."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered: dict[str, str] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle automatic Bluetooth discovery."""
        await self.async_set_unique_id(format_mac(discovery_info.address))
        self._abort_if_unique_id_configured()
        self._discovery_info = discovery_info
        self.context["title_placeholders"] = {
            "name": discovery_info.name or LOCAL_NAME,
            "address": discovery_info.address,
        }
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a discovered scale."""
        assert self._discovery_info is not None
        if user_input is not None:
            return self.async_create_entry(
                title=f"Wyze Scale ({self._discovery_info.address})",
                data={CONF_ADDRESS: self._discovery_info.address},
            )
        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={
                "name": self._discovery_info.name or LOCAL_NAME,
                "address": self._discovery_info.address,
            },
        )

    def _discovered_scales(self, exclude: set[str | None]) -> dict[str, str]:
        """Currently-visible scales, minus the excluded unique ids."""
        discovered: dict[str, str] = {}
        for service_info in bluetooth.async_discovered_service_info(self.hass):
            if not _is_wyze_scale(service_info):
                continue
            if format_mac(service_info.address) in exclude:
                continue
            discovered[service_info.address] = (
                f"{service_info.name or LOCAL_NAME} ({service_info.address})"
            )
        return discovered

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user pick from currently-visible scales."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(format_mac(address), raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=f"Wyze Scale ({address})",
                data={CONF_ADDRESS: address},
            )

        self._discovered = self._discovered_scales(
            set(self._async_current_ids(include_ignore=True))
        )
        if not self._discovered:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_ADDRESS): vol.In(self._discovered)}
            ),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-pick which scale this entry points at (hardware replacement).

        Measurement history is keyed by entry_id, so it survives the swap.
        """
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(format_mac(address))
            if self.unique_id != entry.unique_id:
                # Only a *different* entry owning this address is a conflict.
                self._abort_if_unique_id_configured()
            return self.async_update_reload_and_abort(
                entry,
                unique_id=self.unique_id,
                title=f"Wyze Scale ({address})",
                data_updates={CONF_ADDRESS: address},
            )

        exclude = set(self._async_current_ids(include_ignore=True))
        exclude.discard(entry.unique_id)
        self._discovered = self._discovered_scales(exclude)
        if not self._discovered:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {vol.Required(CONF_ADDRESS): vol.In(self._discovered)}
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "WyzeScaleOptionsFlow":
        return WyzeScaleOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_TYPE_USER: WyzeScaleUserSubentryFlow}


class WyzeScaleOptionsFlow(OptionsFlow):
    """Options: advertisement trigger, cooldown, display unit."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ADVERTISEMENT_TRIGGER,
                        default=options.get(
                            CONF_ADVERTISEMENT_TRIGGER, DEFAULT_ADVERTISEMENT_TRIGGER
                        ),
                    ): bool,
                    vol.Required(
                        CONF_SYNC_COOLDOWN,
                        default=options.get(CONF_SYNC_COOLDOWN, DEFAULT_SYNC_COOLDOWN),
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_SYNC_COOLDOWN, max=MAX_SYNC_COOLDOWN),
                    ),
                    vol.Required(
                        CONF_FALLBACK_INTERVAL,
                        default=options.get(
                            CONF_FALLBACK_INTERVAL, DEFAULT_FALLBACK_INTERVAL
                        ),
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=0, max=MAX_FALLBACK_INTERVAL),
                    ),
                    vol.Required(
                        CONF_DISPLAY_UNIT,
                        default=options.get(CONF_DISPLAY_UNIT, UNIT_OPTION_NONE),
                    ): vol.In([UNIT_OPTION_NONE, UNIT_OPTION_KG, UNIT_OPTION_LB]),
                }
            ),
        )
