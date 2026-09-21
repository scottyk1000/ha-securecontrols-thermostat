from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import schedule as sched
from .api import ApiError, CannotConnect, InvalidAuth, ServerRejected
from .const import DOMAIN, UPDATE_INTERVAL_SECS

_LOGGER = logging.getLogger(__name__)

# Item map for block SI:15 (one block per heating zone; block "I" = zone/slot)
ITEM_TARGET = 1  # target_c (deci °C)
ITEM_AMBIENT = 2  # ambient_c (probe, deci °C)
ITEM_HVAC = 3  # flag written with OT:1; single-zone thermostats: 0 = off, 1 = heat
ITEM_PRESET = 6  # presets (1 = away, 2 = home) — reported on the first zone only
ITEM_HUMID = 8  # humidity %RH (255 = zone sensor has no humidity probe)
ITEM_NEXT_TIME = 9  # next scheduled change (minutes since local midnight)
ITEM_NEXT_VALUE = 10  # next scheduled target temp (deci °C)
ITEM_FROST = 11  # frost_c (deci °C) — first zone only
THERMOSTAT_STATE_BLOCK = 15

# Block SI:16 (hot water channel on H3747/C1727; block "I" = channel)
HOT_WATER_STATE_BLOCK = 16
ITEM_HW_BOOST = 4  # 1 while boosting (OT:2, D = boost length in minutes)
ITEM_HW_NEXT_TIME = 9  # next change / boost end (minutes since local midnight)
ITEM_HW_NEXT_STATE = 10  # state after the next change (0 = off, 1 = on)

# zones.read (49/11) zone types
ZONE_TYPE_HEATING = 0
ZONE_TYPE_HOT_WATER = 1

MIN_VALID_DECI_TEMP = -500
MAX_VALID_DECI_TEMP = 5000
HUMIDITY_NOT_FITTED = 255
PRESET_AWAY = 1
PRESET_HOME = 2
MINUTES_PER_DAY = 1440
PROGRAM_REFRESH_SECS = 15 * 60
WRITE_SETTLE_SECS = 5  # after a write that timed out, wait before checking what was stored

KIND_HEATING = "heating"
KIND_HOT_WATER = "hot_water"


class ThermoCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that polls over the client's persistent WebSocket.

    ``data`` layout::

        {
          # legacy single-thermostat keys (mirror the first heating zone)
          "hvac", "preset", "target_c", "ambient_c", "humidity",
          "next_change_mins", "next_target_c", "frost_c",
          # every zone/channel, keyed by slot number
          "zones": {1: {"kind": "heating", "name": "Downstairs", ...},
                    3: {"kind": "hot_water", "name": "Hot water", ...}, ...},
        }
    """

    def __init__(self, hass: HomeAssistant, client) -> None:
        super().__init__(
            hass,
            logger=_LOGGER,
            name=f"{DOMAIN}_coordinator",
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECS),
        )
        self.client = client
        self._state_cache: dict[str, Any] = {}
        self.topology: dict[int, dict[str, Any]] | None = None
        self._programs: dict[int, list[dict[str, Any]]] = {}
        self._programs_read_at: float | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Poll path: fetch a full snapshot with state_read()."""
        try:
            if getattr(self, "topology", None) is None:
                self.topology = await self._read_topology()
            raw = await self.client.state_read()
            await self._maybe_refresh_programs()
        except InvalidAuth as err:
            # Tell Home Assistant not to schedule further coordinator polls.
            raise ConfigEntryAuthFailed(str(err)) from err
        parsed = self._parse_state_read(raw)
        self._state_cache = parsed
        return parsed

    # ---------- topology & schedules ----------

    async def _read_topology(self) -> dict[int, dict[str, Any]]:
        """zones.read (49/11): names and types of each zone. Empty on single thermostats."""
        try:
            raw = await self.client.zones_read()
        except ServerRejected as err:
            # Plain thermostats may not implement zones.read; transport errors propagate
            # so the poll fails and topology is retried next time.
            _LOGGER.debug("zones.read not available (%s); assuming a single thermostat", err)
            return {}
        topo: dict[int, dict[str, Any]] = {}
        if isinstance(raw, list):
            for z in raw:
                if not isinstance(z, dict) or z.get("ZN") is None:
                    continue
                zn = self._int_or_none(z.get("ZN"))
                if zn is None:
                    continue
                zt = self._int_or_none(z.get("ZT"))
                topo[zn] = {
                    "kind": KIND_HOT_WATER if zt == ZONE_TYPE_HOT_WATER else KIND_HEATING,
                    "name": (z.get("ZNM") or "").strip() or None,
                    "channel": self._int_or_none(z.get("CN")),
                    "sensor_type": self._int_or_none(z.get("DT")),
                }
        _LOGGER.debug("Secure Controls zone topology: %s", topo)
        return topo

    async def _maybe_refresh_programs(self) -> None:
        """Weekly programs: shown as calendars, and tell us if hot water is on right now.

        Re-read every 15 minutes so edits made in the app or on the display show up.
        """
        slots = sorted((self.topology or {}).keys())
        if not hasattr(self, "_programs"):
            self._programs, self._programs_read_at = {}, None
        if not slots:
            return
        last = self._programs_read_at
        if last is not None and time.monotonic() - last < PROGRAM_REFRESH_SECS:
            return
        for slot in slots:
            try:
                raw = await self.client.program_read(slot)
            except ServerRejected as err:
                _LOGGER.debug("program.read(%s) failed: %s", slot, err)
                continue
            if isinstance(raw, list) and raw and isinstance(raw[0], dict):
                self._programs[slot] = raw[0].get("D") or []
        self._programs_read_at = time.monotonic()

    # ---------- parsing helpers ----------

    def _parse_state_read(self, r: Any) -> dict[str, Any]:
        """
        Normalize 3/1 state.read() into a dict for entities.
        Shape of R: {"V":[{"I":<slot>,"SI":<block>,"V":[{I,V,OT,D},...],"S":0},...]}
        """
        state: dict[str, Any] = {
            "hvac": None,  # int flag (item 3)
            "preset": None,  # str: "away" | "home"
            "target_c": None,  # float
            "ambient_c": None,  # float
            "humidity": None,  # %
            "next_change_mins": None,  # int, minutes since local midnight
            "next_target_c": None,  # float
            "frost_c": None,  # float
            "zones": {},
        }

        vec = r.get("V") if isinstance(r, dict) else None
        if not isinstance(vec, list):
            return state

        topology = getattr(self, "topology", None) or {}
        zones: dict[int, dict[str, Any]] = state["zones"]
        for block in vec:
            if not isinstance(block, dict):
                continue
            si = block.get("SI")
            slot = self._int_or_none(block.get("I")) or 1
            items = {it.get("I"): it for it in (block.get("V") or []) if isinstance(it, dict)}
            if si == THERMOSTAT_STATE_BLOCK:
                zone = self._parse_heating(items)
            elif si == HOT_WATER_STATE_BLOCK and (
                topology.get(slot, {}).get("kind") == KIND_HOT_WATER
            ):
                zone = self._parse_hot_water(items, getattr(self, "_programs", {}).get(slot))
            else:
                continue
            zone["slot"] = slot
            week = sched.parse_wire(getattr(self, "_programs", {}).get(slot))
            zone["schedule"] = week
            if week and zone["kind"] == KIND_HEATING:
                value = sched.value_at(week, dt_util.now())
                zone["scheduled_target_c"] = None if value is None else value / 10
            info = topology.get(slot, {})
            zone["name"] = info.get("name")
            zone["sensor_type"] = info.get("sensor_type")
            zones[slot] = zone

        heating = sorted(s for s, z in zones.items() if z["kind"] == KIND_HEATING)
        if heating:
            first = zones[heating[0]]
            for key in (
                "hvac",
                "preset",
                "target_c",
                "ambient_c",
                "humidity",
                "next_change_mins",
                "next_target_c",
                "frost_c",
            ):
                state[key] = first.get(key)
        return state

    def _parse_heating(self, items: dict[Any, dict[str, Any]]) -> dict[str, Any]:
        def val(i: int) -> Any:
            return items.get(i, {}).get("V")

        humidity = self._int_or_none(val(ITEM_HUMID))
        return {
            "kind": KIND_HEATING,
            "hvac": self._int_or_none(val(ITEM_HVAC)),
            "preset": self._preset_name(self._int_or_none(val(ITEM_PRESET))),
            "target_c": self._deci_to_c(val(ITEM_TARGET)),
            "ambient_c": self._maybe_deci_temp(val(ITEM_AMBIENT)),
            "humidity": None if humidity == HUMIDITY_NOT_FITTED else humidity,
            "next_change_mins": self._int_or_none(val(ITEM_NEXT_TIME)),
            "next_target_c": self._deci_to_c(val(ITEM_NEXT_VALUE)),
            "frost_c": self._deci_to_c(val(ITEM_FROST)),
        }

    def _parse_hot_water(
        self, items: dict[Any, dict[str, Any]], program: list[dict[str, Any]] | None
    ) -> dict[str, Any]:
        boost = items.get(ITEM_HW_BOOST, {})
        boost_active = self._int_or_none(boost.get("V")) == 1
        scheduled_on = self.program_state_at(program, dt_util.now()) if program else None
        if boost_active:
            is_on: bool | None = True
        else:
            is_on = scheduled_on
        return {
            "kind": KIND_HOT_WATER,
            "boost_active": boost_active,
            "boost_minutes": self._int_or_none(boost.get("D")) if boost_active else None,
            "next_change_mins": self._int_or_none(items.get(ITEM_HW_NEXT_TIME, {}).get("V")),
            "next_state_on": self._int_or_none(items.get(ITEM_HW_NEXT_STATE, {}).get("V")) == 1,
            "scheduled_on": scheduled_on,
            "is_on": is_on,
        }

    # ---------- time helpers (public: used by entities & tests) ----------

    @staticmethod
    def minute_of_day_to_datetime(minute: int | None, now: datetime) -> datetime | None:
        """Next local datetime at <minute> past midnight (today if still ahead, else tomorrow)."""
        if minute is None or not 0 <= minute < MINUTES_PER_DAY:
            return None
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        candidate = midnight + timedelta(minutes=minute)
        if candidate < now - timedelta(minutes=1):
            candidate += timedelta(days=1)
        return candidate

    @staticmethod
    def program_state_at(program: list[dict[str, Any]], now: datetime) -> bool | None:
        """On/off state of an on/off weekly program (Mon→Sun, 6 switch points a day)."""
        week = sched.parse_wire(program)
        if not week or not any(week.values()):
            return None
        value = sched.value_at(week, now)
        return None if value is None else value == 1

    # ---------- schedules (get_schedule / set_schedule) ----------

    def zone_kind(self, slot: int) -> str:
        return (self.topology or {}).get(slot, {}).get("kind", KIND_HEATING)

    async def async_read_schedule(self, slot: int) -> dict[str, list[sched.Point]]:
        """Fresh read of a zone's weekly program from the programmer."""
        try:
            raw = await self.client.program_read(slot)
        except ServerRejected as err:
            raise HomeAssistantError(f"The programmer rejected the schedule read: {err}") from err
        except ApiError as err:
            raise HomeAssistantError(f"Could not read the schedule for zone {slot}: {err}") from err
        entries = None
        if isinstance(raw, list) and raw and isinstance(raw[0], dict):
            entries = raw[0].get("D")
        week = sched.parse_wire(entries)
        if week is None:
            raise HomeAssistantError(f"Zone {slot} did not return a weekly schedule")
        self._programs[slot] = entries
        # Show the fresh read straight away (calendars, schedule card) rather than
        # waiting for the next poll.
        zone = ((getattr(self, "data", None) or {}).get("zones") or {}).get(slot)
        if zone is not None and zone.get("schedule") != week:
            zone["schedule"] = week
            self.async_update_listeners()
        return week

    async def async_write_schedule(
        self, slot: int, changes: dict[str, list[sched.Point]]
    ) -> dict[str, list[sched.Point]]:
        """Replace the given days of a zone's program, then read back to confirm."""
        kind = self.zone_kind(slot)
        current = await self.async_read_schedule(slot)
        merged = sched.merge(current, changes)
        try:
            entries = sched.build_wire(merged, kind)
        except sched.ScheduleError as err:
            raise HomeAssistantError(f"Zone {slot}: {err}") from err
        if entries == self._programs.get(slot):
            return current  # nothing to change
        _LOGGER.info(
            "Writing weekly schedule for zone %s: %s", slot, sched.to_friendly(merged, kind)
        )
        write_error: ApiError | None = None
        try:
            await self.client.program_write(slot, entries)
        except ServerRejected as err:
            raise HomeAssistantError(f"The programmer rejected the schedule: {err}") from err
        except CannotConnect as err:
            # A schedule write can outlast the reply timeout, or the cloud may drop the
            # connection after accepting it. Check what the programmer actually stored.
            _LOGGER.warning("Schedule write for zone %s did not confirm (%s); checking", slot, err)
            write_error = err
            await asyncio.sleep(WRITE_SETTLE_SECS)
        stored = await self.async_read_schedule(slot)
        if stored != sched.parse_wire(entries):
            if write_error is not None:
                raise HomeAssistantError(
                    f"Zone {slot}: the schedule could not be sent ({write_error}). "
                    "Nothing was changed; please try again."
                )
            raise HomeAssistantError(
                f"Zone {slot}: the programmer stored a different schedule than was sent. "
                f"Stored: {sched.to_friendly(stored, kind)}"
            )
        await self.async_request_refresh()
        return stored

    # ---------- unit & parse helpers ----------

    @staticmethod
    def _deci_to_c(v: int | None) -> float | None:
        if v is None:
            return None
        try:
            return float(v) / 10.0
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _maybe_deci_temp(v: int | None) -> float | None:
        if v is None:
            return None
        try:
            iv = int(v)
        except (TypeError, ValueError, OverflowError):
            return None
        if MIN_VALID_DECI_TEMP <= iv <= MAX_VALID_DECI_TEMP:
            return iv / 10.0
        return None

    @staticmethod
    def _int_or_none(v: Any) -> int | None:
        try:
            return int(v)
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _preset_name(code: int | None) -> str | None:
        if code == PRESET_AWAY:
            return "away"
        if code == PRESET_HOME:
            return "home"
        return None
