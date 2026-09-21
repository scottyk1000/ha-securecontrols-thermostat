from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity

from .const import CONF_GATEWAY_GMI, DOMAIN
from .coordinator import KIND_HOT_WATER, ThermoCoordinator
from .entity import SecureZoneEntity, zone_slots


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: ThermoCoordinator = data["coordinator"]
    gmi: str = entry.data[CONF_GATEWAY_GMI]
    async_add_entities(
        HotWaterActiveSensor(coordinator, data["client"], gmi, slot)
        for slot in zone_slots(coordinator, KIND_HOT_WATER)
    )


class HotWaterActiveSensor(SecureZoneEntity, BinarySensorEntity):
    """Whether the hot water channel is on now (schedule or boost)."""

    _attr_name = "Heating"
    _attr_device_class = BinarySensorDeviceClass.HEAT

    def __init__(self, coordinator: ThermoCoordinator, client, gmi: str, slot: int) -> None:
        super().__init__(coordinator, client, gmi, slot, "hot_water_on")

    @property
    def is_on(self) -> bool | None:
        return self.zone.get("is_on")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        z = self.zone
        return {"boost_active": z.get("boost_active"), "scheduled_on": z.get("scheduled_on")}
