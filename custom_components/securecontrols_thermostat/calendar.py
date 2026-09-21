"""Each zone's weekly schedule as a calendar, plus get_schedule / set_schedule actions."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform
from homeassistant.util import dt as dt_util

from . import schedule as sched
from .const import CONF_GATEWAY_GMI, DOMAIN, SERVICE_GET_SCHEDULE, SERVICE_SET_SCHEDULE
from .coordinator import KIND_HOT_WATER, ThermoCoordinator
from .entity import SecureZoneEntity

LOOKAHEAD = timedelta(days=8)

SET_SCHEDULE_SCHEMA = {
    vol.Optional("schedule"): vol.All(dict, vol.Length(min=1)),
    vol.Optional("days"): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional("periods"): vol.All(cv.ensure_list, [dict]),
}


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: ThermoCoordinator = data["coordinator"]
    gmi: str = entry.data[CONF_GATEWAY_GMI]
    slots = sorted((coordinator.topology or {}).keys())
    async_add_entities(ZoneScheduleCalendar(coordinator, data["client"], gmi, s) for s in slots)

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_GET_SCHEDULE, {}, "async_get_schedule", supports_response=SupportsResponse.ONLY
    )
    platform.async_register_entity_service(
        SERVICE_SET_SCHEDULE,
        SET_SCHEDULE_SCHEMA,
        "async_set_schedule",
        supports_response=SupportsResponse.OPTIONAL,
    )


class ZoneScheduleCalendar(SecureZoneEntity, CalendarEntity):
    """A zone's weekly program on the programmer, shown as calendar events."""

    _attr_name = "Schedule"
    _attr_icon = "mdi:calendar-clock"
    # The schedule card reads these; keep them out of the recorder database.
    _unrecorded_attributes = frozenset({"schedule", "zone_type", "zone_name", "zone"})

    def __init__(self, coordinator: ThermoCoordinator, client, gmi: str, slot: int) -> None:
        super().__init__(coordinator, client, gmi, slot, "schedule")

    @property
    def _kind(self) -> str:
        return self.coordinator.zone_kind(self._slot)

    @property
    def _week(self) -> dict[str, list[sched.Point]] | None:
        return self.zone.get("schedule")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        week = self._week
        return {
            "zone": self._slot,
            "zone_name": self.zone_name,
            "zone_type": self._kind,
            "schedule": sched.to_friendly(week, self._kind) if week else None,
        }

    # ---------- calendar ----------

    def _events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        week = self._week
        if not week:
            return []
        events = []
        for seg_start, seg_end, value in sched.segments(week, start, end):
            if self._kind == KIND_HOT_WATER:
                if value != 1:
                    continue
                summary = f"{self.zone_name} on"
            else:
                summary = f"{self.zone_name} {value / 10:.1f} °C"
            events.append(
                CalendarEvent(
                    start=seg_start,
                    end=seg_end,
                    summary=summary,
                    uid=f"{self.unique_id}_{seg_start.isoformat()}",
                )
            )
        return events

    @property
    def event(self) -> CalendarEvent | None:
        now = dt_util.now()
        events = self._events(now, now + LOOKAHEAD)
        return events[0] if events else None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        tz = dt_util.get_default_time_zone()
        return self._events(start_date.astimezone(tz), end_date.astimezone(tz))

    # ---------- actions ----------

    async def async_get_schedule(self) -> ServiceResponse:
        week = await self.coordinator.async_read_schedule(self._slot)
        return {
            "zone": self.zone_name,
            "type": self._kind,
            "schedule": sched.to_friendly(week, self._kind),
        }

    async def async_set_schedule(
        self,
        schedule: dict[str, Any] | None = None,
        days: list[str] | None = None,
        periods: list[dict[str, Any]] | None = None,
    ) -> ServiceResponse:
        changes = self._parse_changes(schedule, days, periods)
        week = await self.coordinator.async_write_schedule(self._slot, changes)
        return {
            "zone": self.zone_name,
            "type": self._kind,
            "schedule": sched.to_friendly(week, self._kind),
        }

    def _parse_changes(
        self,
        schedule: dict[str, Any] | None,
        days: list[str] | None,
        periods: list[dict[str, Any]] | None,
    ) -> dict[str, list[sched.Point]]:
        if (schedule is None) == (days is None and periods is None):
            raise ServiceValidationError(
                "Give either 'schedule' (days mapped to periods) or 'days' with 'periods'"
            )
        if schedule is None:
            if not days or not periods:
                raise ServiceValidationError("'days' and 'periods' must be given together")
            schedule = {d: periods for d in days}
        changes: dict[str, list[sched.Point]] = {}
        try:
            for key, day_periods in schedule.items():
                points = sched.from_friendly(day_periods, self._kind)
                for day in sched.expand_days(key):
                    changes[day] = points
        except sched.ScheduleError as err:
            raise ServiceValidationError(f"{self.zone_name}: {err}") from err
        return changes
