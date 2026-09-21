from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import ApiError
from .const import DOMAIN
from .coordinator import KIND_HEATING, KIND_HOT_WATER, ThermoCoordinator

LEGACY_SLOT = 1


def zone_slots(coordinator: ThermoCoordinator, kind: str) -> list[int]:
    """Slots of a given kind seen in the latest poll (falls back to slot 1 for a thermostat)."""
    zones = (coordinator.data or {}).get("zones") or {}
    slots = sorted(s for s, z in zones.items() if z.get("kind") == kind)
    if not slots and kind == KIND_HEATING and not zones:
        return [LEGACY_SLOT]
    return slots


def is_multi_zone(coordinator: ThermoCoordinator) -> bool:
    """True for programmers (H3747/C1727) exposing more than one zone/channel."""
    return len((coordinator.data or {}).get("zones") or {}) > 1


class SecureZoneEntity(CoordinatorEntity[ThermoCoordinator]):
    """Base entity bound to one zone/channel (slot) of a gateway.

    Single thermostats keep the original device and unique IDs. Multi-zone
    programmers get one device per zone, linked to the gateway device.
    Slot 1 keeps the original unique IDs so existing entities survive upgrades.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: ThermoCoordinator, client, gmi: str, slot: int, key: str):
        super().__init__(coordinator)
        self.client = client
        self._gmi = gmi
        self._slot = slot
        self._multi = is_multi_zone(coordinator)
        prefix = gmi if slot == LEGACY_SLOT else f"{gmi}_zone{slot}"
        self._attr_unique_id = f"{prefix}_{key}"

    # ---------- data ----------

    @property
    def zone(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        zone = (data.get("zones") or {}).get(self._slot)
        if zone is None and self._slot == LEGACY_SLOT and not data.get("zones"):
            return data  # plain thermostat payload
        return zone or {}

    @property
    def available(self) -> bool:
        return super().available and bool(self.zone)

    @property
    def zone_name(self) -> str:
        name = self.zone.get("name")
        if name:
            return name
        if self.zone.get("kind") == KIND_HOT_WATER:
            return "Hot water"
        return f"Zone {self._slot}"

    async def _command(self, call: Awaitable[Any]) -> Any:
        """Send a command, turning cloud errors into a readable message."""
        try:
            return await call
        except ApiError as err:
            raise HomeAssistantError(
                f"{self.zone_name}: the command was not accepted ({err})"
            ) from err

    # ---------- device ----------

    def _gateway_device(self) -> DeviceInfo:
        ther = getattr(self.client, "thermostat", None)
        sn = getattr(ther, "sn", None) if ther else None
        hn = getattr(ther, "hn", None) if ther else None
        return DeviceInfo(
            identifiers={(DOMAIN, self._gmi)},
            manufacturer="Secure Meters",
            model="Smart programmer" if self._multi else "Thermostat",
            name=(hn or sn or "Secure Thermostat")
            if not self._multi
            else f"Secure programmer {hn or sn or ''}".strip(),
            serial_number=sn,
        )

    @property
    def device_info(self) -> DeviceInfo:
        if not self._multi:
            return self._gateway_device()
        hot_water = self.zone.get("kind") == KIND_HOT_WATER
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._gmi}_zone{self._slot}")},
            manufacturer="Secure Meters",
            model="Hot water channel" if hot_water else "Heating zone",
            name=self.zone_name,
        )
