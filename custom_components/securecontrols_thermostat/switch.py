from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform

from .const import CONF_GATEWAY_GMI, DEFAULT_BOOST_MINUTES, DOMAIN, SERVICE_BOOST
from .coordinator import KIND_HOT_WATER, ThermoCoordinator
from .entity import SecureZoneEntity, zone_slots

MAX_BOOST_MINUTES = 240


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: ThermoCoordinator = data["coordinator"]
    gmi: str = entry.data[CONF_GATEWAY_GMI]
    async_add_entities(
        HotWaterBoostSwitch(coordinator, data["client"], gmi, slot)
        for slot in zone_slots(coordinator, KIND_HOT_WATER)
    )

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_BOOST,
        {
            vol.Required("minutes", default=DEFAULT_BOOST_MINUTES): vol.All(
                cv.positive_int, vol.Range(min=1, max=MAX_BOOST_MINUTES)
            )
        },
        "async_boost",
    )


class HotWaterBoostSwitch(SecureZoneEntity, SwitchEntity):
    """Hot water boost: on = boost (60 min by default), off = cancel boost."""

    _attr_name = "Boost"
    _attr_icon = "mdi:water-boiler"

    def __init__(self, coordinator: ThermoCoordinator, client, gmi: str, slot: int) -> None:
        super().__init__(coordinator, client, gmi, slot, "hot_water_boost")

    @property
    def is_on(self) -> bool:
        return bool(self.zone.get("boost_active"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"boost_minutes": self.zone.get("boost_minutes")}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.async_boost(DEFAULT_BOOST_MINUTES)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._command(self.client.hot_water_cancel_boost(self._slot))
        await self.coordinator.async_request_refresh()

    async def async_boost(self, minutes: int) -> None:
        """Boost for a custom number of minutes (securecontrols_thermostat.boost)."""
        await self._command(self.client.hot_water_boost(self._slot, int(minutes)))
        await self.coordinator.async_request_refresh()
