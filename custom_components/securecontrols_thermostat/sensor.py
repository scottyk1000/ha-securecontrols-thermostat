from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.util import dt as dt_util

from .const import CONF_GATEWAY_GMI, DOMAIN
from .coordinator import KIND_HEATING, KIND_HOT_WATER, ThermoCoordinator
from .entity import SecureZoneEntity, zone_slots


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Secure Controls sensors from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    client = data["client"]
    coordinator: ThermoCoordinator = data["coordinator"]
    gmi: str = entry.data.get(CONF_GATEWAY_GMI) or getattr(
        getattr(client, "thermostat", None), "gmi", "unknown"
    )

    entities: list[SensorEntity] = []
    for slot in zone_slots(coordinator, KIND_HEATING):
        entities += [
            CurrentTempSensor(coordinator, client, gmi, slot),
            TargetTempSensor(coordinator, client, gmi, slot),
            NextChangeTimeSensor(coordinator, client, gmi, slot),
            NextTargetTempSensor(coordinator, client, gmi, slot),
        ]
        zone = (coordinator.data or {}).get("zones", {}).get(slot, {})
        if zone.get("humidity") is not None:
            entities.append(HumiditySensor(coordinator, client, gmi, slot))
    for slot in zone_slots(coordinator, KIND_HOT_WATER):
        entities.append(NextChangeTimeSensor(coordinator, client, gmi, slot))
    async_add_entities(entities)


class _ZoneSensor(SecureZoneEntity, SensorEntity):
    """Sensor for one zone; ``_field`` names the coordinator zone key."""

    _field: str
    _key: str

    def __init__(self, coordinator: ThermoCoordinator, client, gmi: str, slot: int) -> None:
        super().__init__(coordinator, client, gmi, slot, self._key)

    @property
    def native_value(self) -> Any:
        return self.zone.get(self._field)


class CurrentTempSensor(_ZoneSensor):
    """Current measured ambient temperature (°C)."""

    _attr_name = "Current Temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _field = "ambient_c"
    _key = "current_temp_c"


class TargetTempSensor(_ZoneSensor):
    """Current active target temperature (°C)."""

    _attr_name = "Target Temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _field = "target_c"
    _key = "target_temp_c"


class NextTargetTempSensor(_ZoneSensor):
    """The next scheduled target temperature (°C)."""

    _attr_name = "Next Target Temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _field = "next_target_c"
    _key = "next_target_c"


class HumiditySensor(_ZoneSensor):
    """Relative humidity from the zone's display/sensor, where fitted."""

    _attr_name = "Humidity"
    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _field = "humidity"
    _key = "humidity"


class NextChangeTimeSensor(_ZoneSensor):
    """When the zone's schedule next changes (for hot water: also when a boost ends).

    The device reports this as minutes since local midnight.
    """

    _attr_name = "Next Schedule Change"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _field = "next_change_mins"
    _key = "next_change"

    @property
    def native_value(self) -> datetime | None:
        return ThermoCoordinator.minute_of_day_to_datetime(
            self.zone.get("next_change_mins"), dt_util.now()
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        z = self.zone
        if z.get("kind") == KIND_HOT_WATER:
            return {"next_state": "on" if z.get("next_state_on") else "off"}
        return {"next_target_c": z.get("next_target_c")}
