from __future__ import annotations

from typing import Any, ClassVar

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    PRESET_NONE,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature

from .const import CONF_GATEWAY_GMI, DOMAIN
from .coordinator import KIND_HEATING, ThermoCoordinator
from .entity import SecureZoneEntity, zone_slots


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    client = data["client"]
    coordinator: ThermoCoordinator = data["coordinator"]  # created in __init__.py
    gmi: str = entry.data[CONF_GATEWAY_GMI]

    async_add_entities(
        SecureThermostatEntity(coordinator, client, gmi, slot)
        for slot in zone_slots(coordinator, KIND_HEATING)
    )


class SecureThermostatEntity(SecureZoneEntity, ClimateEntity):
    """One heating zone.

    Plain thermostats keep the original heat/off behaviour driven by item 3.
    On multi-zone programmers item 3 does not reflect heat/off, so zones are
    heat-only and simply follow their schedule unless the target is changed.
    """

    _attr_preset_modes: ClassVar[list[str]] = ["away", "home"]
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = 5.0
    _attr_max_temp = 30.0
    _attr_target_temperature_step = 0.5
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, coordinator: ThermoCoordinator, client, gmi: str, slot: int) -> None:
        super().__init__(coordinator, client, gmi, slot, "climate")
        # Zone devices are named after the zone, so the climate entity takes the device name.
        self._attr_name = None if self._multi else "Thermostat"
        # Away/home is reported (and settable) on the first zone only.
        self._has_preset = self.zone.get("preset") is not None or not self._multi
        features = ClimateEntityFeature.TARGET_TEMPERATURE
        if self._has_preset:
            features |= ClimateEntityFeature.PRESET_MODE
        self._attr_supported_features = features
        self._attr_hvac_modes = [HVACMode.HEAT] if self._multi else [HVACMode.HEAT, HVACMode.OFF]

    # ---------- state ----------

    @property
    def hvac_mode(self) -> HVACMode:
        if self._multi:
            return HVACMode.HEAT
        return HVACMode.HEAT if self.zone.get("hvac") == 1 else HVACMode.OFF

    @property
    def hvac_action(self) -> HVACAction | None:
        if self._multi:
            return None  # the cloud does not report per-zone demand
        hvac_val = self.zone.get("hvac")
        if hvac_val == 1:
            return HVACAction.HEATING
        if hvac_val == 0:
            return HVACAction.IDLE
        return None

    @property
    def current_temperature(self) -> float | None:
        return self.zone.get("ambient_c")

    @property
    def target_temperature(self) -> float | None:
        return self.zone.get("target_c")

    @property
    def current_humidity(self) -> float | None:
        return self.zone.get("humidity")

    @property
    def preset_mode(self) -> str | None:
        if not self._has_preset:
            return None
        return self.zone.get("preset") or PRESET_NONE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        z = self.zone
        return {
            "zone": self._slot,
            "scheduled_temperature": z.get("scheduled_target_c"),
            "next_target_temperature": z.get("next_target_c"),
            "frost_protection_temperature": z.get("frost_c"),
        }

    # ---------- commands ----------

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Legacy single-thermostat behaviour; programmer zones are heat-only."""
        if self._multi or self.current_temperature is None:
            return
        if hvac_mode == HVACMode.HEAT:
            await self._command(self.client.set_target_temp(self.current_temperature + 2.0))
        else:
            await self._command(self.client.set_target_temp(self.current_temperature - 2.0))
        await self.coordinator.async_request_refresh()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if ATTR_TEMPERATURE in kwargs:
            target = float(kwargs[ATTR_TEMPERATURE])
            await self._command(self.client.set_target_temp(target, slot=self._slot))
            await self.coordinator.async_request_refresh()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set the away/home preset."""
        if preset_mode not in self._attr_preset_modes:
            return
        await self._command(self.client.set_preset(preset_mode))
        await self.coordinator.async_request_refresh()
