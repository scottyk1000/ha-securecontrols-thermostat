# ruff: noqa: PLR2004
"""Load the integration into a real Home Assistant instance with captured H3747 data."""

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.securecontrols_thermostat as integration
from custom_components.securecontrols_thermostat.api import Thermostat
from custom_components.securecontrols_thermostat.const import (
    CONF_EMAIL,
    CONF_GATEWAY_GMI,
    CONF_PASSWORD,
    DOMAIN,
)

FIXTURE = json.loads(
    (Path(__file__).parent.parent / "tests" / "fixtures" / "h3747.json").read_text()
)
GMI = "1234567"


class FakeClient:
    """Stands in for SecureControlsClient; replays captured responses, records writes."""

    state = FIXTURE["state_read"]

    def __init__(self, _session):
        self.calls: list[tuple] = []
        self.programs = json.loads(json.dumps(FIXTURE["programs"]))
        self.ignore_writes = False
        self.write_error = None  # exception to raise from program_write (after storing)
        self.thermostat = None
        self.preferred_gmi = None
        FakeClient.instance = self

    async def login(self, email, password):
        self.thermostat = Thermostat(gmi=GMI, sn="H3747SN", hn="H3747SN")

    async def zones_read(self):
        return FIXTURE["zones_read"]

    async def state_read(self):
        return FakeClient.state

    async def program_read(self, index):
        return self.programs[str(index)]

    async def program_write(self, index, entries):
        self.calls.append(("program_write", index, len(entries)))
        if not self.ignore_writes:
            self.programs[str(index)] = [{"I": index, "D": entries}]
        if self.write_error:
            raise self.write_error
        return 0

    async def set_target_temp(self, celsius, slot=1):
        self.calls.append(("target", slot, celsius))

    async def set_preset(self, preset):
        self.calls.append(("preset", preset))

    async def hot_water_boost(self, slot, minutes):
        self.calls.append(("boost", slot, minutes))

    async def hot_water_cancel_boost(self, slot):
        self.calls.append(("cancel", slot))

    async def disconnect(self):
        pass


@pytest.fixture
async def setup(hass: HomeAssistant, freezer: FrozenDateTimeFactory, monkeypatch):
    await hass.config.async_set_time_zone("Europe/London")
    freezer.move_to(datetime(2026, 9, 21, 11, 0, tzinfo=ZoneInfo("Europe/London")))
    FakeClient.state = FIXTURE["state_read"]
    monkeypatch.setattr(integration, "SecureControlsClient", FakeClient)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=GMI,
        data={CONF_EMAIL: "a@b.c", CONF_PASSWORD: "x", CONF_GATEWAY_GMI: GMI},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_entities_created_for_each_zone(hass: HomeAssistant, setup):
    ent_reg = er.async_get(hass)
    entities = sorted(e.entity_id for e in ent_reg.entities.values())
    print("\n".join(entities))

    climates = [e for e in entities if e.startswith("climate.")]
    assert climates == ["climate.downstairs", "climate.ptd", "climate.upstairs"]

    down = hass.states.get("climate.downstairs")
    assert down.state == "heat"
    assert down.attributes["temperature"] == 12.0
    assert down.attributes["current_temperature"] == 19.0
    assert down.attributes["current_humidity"] == 71
    assert down.attributes["preset_mode"] == "home"

    up = hass.states.get("climate.upstairs")
    assert (up.attributes["temperature"], up.attributes["current_temperature"]) == (14.0, 23.0)
    assert "preset_mode" not in up.attributes

    kitchen = hass.states.get("climate.ptd")
    assert (kitchen.attributes["temperature"], kitchen.attributes["current_temperature"]) == (
        17.0,
        18.0,
    )

    # Humidity only where the zone's sensor has it
    assert "sensor.downstairs_humidity" in entities
    assert "sensor.ptd_humidity" in entities
    assert "sensor.upstairs_humidity" not in entities

    assert hass.states.get("sensor.downstairs_next_schedule_change").state == (
        "2026-09-21T12:00:00+00:00"  # 13:00 BST
    )
    assert hass.states.get("switch.hot_water_boost").state == "off"
    assert hass.states.get("binary_sensor.hot_water_heating").state == "off"
    assert hass.states.get("sensor.hot_water_next_schedule_change").state == (
        "2026-09-21T11:00:00+00:00"  # 12:00 BST
    )

    dev_reg = dr.async_get(hass)
    devices = {d.name: d for d in dev_reg.devices.values()}
    assert {"Downstairs", "Upstairs", "Hot water", "PTD"} <= set(devices)
    programmer = next(d for d in dev_reg.devices.values() if (DOMAIN, GMI) in d.identifiers)
    assert devices["Downstairs"].via_device_id == programmer.id


async def test_controls_send_the_right_commands(hass: HomeAssistant, setup):
    client = FakeClient.instance

    await hass.services.async_call(
        "climate",
        "set_temperature",
        {ATTR_ENTITY_ID: "climate.upstairs", "temperature": 19.5},
        blocking=True,
    )
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {ATTR_ENTITY_ID: "climate.ptd", "temperature": 18},
        blocking=True,
    )
    await hass.services.async_call(
        "switch", "turn_on", {ATTR_ENTITY_ID: "switch.hot_water_boost"}, blocking=True
    )
    await hass.services.async_call(
        DOMAIN, "boost", {ATTR_ENTITY_ID: "switch.hot_water_boost", "minutes": 30}, blocking=True
    )
    await hass.services.async_call(
        "switch", "turn_off", {ATTR_ENTITY_ID: "switch.hot_water_boost"}, blocking=True
    )
    await hass.services.async_call(
        "climate",
        "set_preset_mode",
        {ATTR_ENTITY_ID: "climate.downstairs", "preset_mode": "away"},
        blocking=True,
    )

    assert client.calls == [
        ("target", 2, 19.5),
        ("target", 4, 18.0),
        ("boost", 3, 60),
        ("boost", 3, 30),
        ("cancel", 3),
        ("preset", "away"),
    ]


async def test_boost_state_is_reflected(hass: HomeAssistant, setup):
    state = json.loads(json.dumps(FIXTURE["state_read"]))
    hw = next(b for b in state["V"] if b["SI"] == 16)
    for item in hw["V"]:
        if item["I"] == 4:
            item.update({"V": 1, "OT": 2, "D": 60})
        if item["I"] == 9:
            item["V"] = 722
    FakeClient.state = state

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get("switch.hot_water_boost").state == "on"
    assert hass.states.get("switch.hot_water_boost").attributes["boost_minutes"] == 60
    assert hass.states.get("binary_sensor.hot_water_heating").state == "on"
    assert hass.states.get("sensor.hot_water_next_schedule_change").state == (
        "2026-09-21T11:02:00+00:00"  # boost ends 12:02 BST
    )


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError  # noqa: E402


async def test_schedule_calendars(hass: HomeAssistant, setup):
    ent_reg = er.async_get(hass)
    calendars = sorted(e.entity_id for e in ent_reg.entities.values() if e.domain == "calendar")
    assert calendars == [
        "calendar.downstairs_schedule",
        "calendar.hot_water_schedule",
        "calendar.ptd_schedule",
        "calendar.upstairs_schedule",
    ]
    # 11:00 Monday: Downstairs is in its 12 °C block (06:00 -> 18:00)
    down = hass.states.get("calendar.downstairs_schedule")
    assert down.attributes["message"] == "Downstairs 12.0 °C"
    assert down.state == "on"
    # Hot water is off now; the next "on" block is shown with state off
    hw = hass.states.get("calendar.hot_water_schedule")
    assert hw.state == "off"
    assert hw.attributes["message"] == "Hot water on"

    result = await hass.services.async_call(
        "calendar",
        "get_events",
        {
            ATTR_ENTITY_ID: "calendar.hot_water_schedule",
            "start_date_time": "2026-09-21T00:00:00+01:00",
            "end_date_time": "2026-09-22T00:00:00+01:00",
        },
        blocking=True,
        return_response=True,
    )
    events = result["calendar.hot_water_schedule"]["events"]
    assert [(e["start"], e["end"]) for e in events] == [
        ("2026-09-21T05:40:00+01:00", "2026-09-21T06:40:00+01:00")
    ]


async def test_get_schedule(hass: HomeAssistant, setup):
    result = await hass.services.async_call(
        DOMAIN,
        "get_schedule",
        {ATTR_ENTITY_ID: "calendar.downstairs_schedule"},
        blocking=True,
        return_response=True,
    )
    body = result["calendar.downstairs_schedule"]
    assert body["type"] == "heating"
    assert body["schedule"]["monday"] == [
        {"start": "06:00", "temperature": 12.0},
        {"start": "18:00", "temperature": 16.0},
        {"start": "22:00", "temperature": 12.0},
    ]


async def test_set_schedule_changes_only_given_days(hass: HomeAssistant, setup):
    client = FakeClient.instance
    before = json.loads(json.dumps(client.programs["4"]))
    periods = [
        {"start": "06:30", "temperature": 20},
        {"start": "08:30", "temperature": 16},
        {"start": "17:30", "temperature": 20.5},
        {"start": "22:30", "temperature": 15},
    ]
    result = await hass.services.async_call(
        DOMAIN,
        "set_schedule",
        {ATTR_ENTITY_ID: "calendar.ptd_schedule", "days": ["weekdays"], "periods": periods},
        blocking=True,
        return_response=True,
    )
    assert ("program_write", 4, 42) in client.calls
    stored = result["calendar.ptd_schedule"]["schedule"]
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday"):
        assert stored[day] == [
            {"start": "06:30", "temperature": 20.0},
            {"start": "08:30", "temperature": 16.0},
            {"start": "17:30", "temperature": 20.5},
            {"start": "22:30", "temperature": 15.0},
        ]
    # Weekend untouched, byte for byte
    after = client.programs["4"][0]["D"]
    assert after[30:] == before[0]["D"][30:]
    # Every heating day still carries 6 switch points
    assert all(e["O"] != 65535 for e in after)


async def test_set_schedule_hot_water_full_week_mapping(hass: HomeAssistant, setup):
    client = FakeClient.instance
    await hass.services.async_call(
        DOMAIN,
        "set_schedule",
        {
            ATTR_ENTITY_ID: "calendar.hot_water_schedule",
            "schedule": {
                "all": [{"start": "06:00", "state": "on"}, {"start": "07:00", "state": "off"}],
                "sunday": [{"start": "08:00", "state": "on"}, {"start": "09:30", "state": "off"}],
            },
        },
        blocking=True,
    )
    wire = client.programs["3"][0]["D"]
    assert wire[:6] == [
        {"O": 360, "T": 1},
        {"O": 420, "T": 0},
        {"O": 65535, "T": 65535},
        {"O": 65535, "T": 65535},
        {"O": 65535, "T": 65535},
        {"O": 65535, "T": 65535},
    ]
    assert wire[36:38] == [{"O": 480, "T": 1}, {"O": 570, "T": 0}]


@pytest.mark.parametrize(
    "periods",
    [
        [{"start": "06:00", "temperature": 31}],
        [{"start": "06:00", "temperature": 20.3}],
        [{"start": "08:00", "temperature": 20}, {"start": "06:00", "temperature": 15}],
        [{"start": "25:00", "temperature": 20}],
        [{"start": f"0{h}:00", "temperature": 20} for h in range(7)],
    ],
)
async def test_set_schedule_rejects_invalid(hass: HomeAssistant, setup, periods):
    client = FakeClient.instance
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "set_schedule",
            {ATTR_ENTITY_ID: "calendar.downstairs_schedule", "days": "monday", "periods": periods},
            blocking=True,
        )
    assert not [c for c in client.calls if c[0] == "program_write"]


async def test_set_schedule_detects_programmer_not_storing(hass: HomeAssistant, setup):
    client = FakeClient.instance
    client.ignore_writes = True
    with pytest.raises(HomeAssistantError, match="stored a different schedule"):
        await hass.services.async_call(
            DOMAIN,
            "set_schedule",
            {
                ATTR_ENTITY_ID: "calendar.downstairs_schedule",
                "days": "monday",
                "periods": [{"start": "07:00", "temperature": 19}],
            },
            blocking=True,
        )


# ---------------------------------------------------------------------------
# Blueprints (loaded from the repo and run as real automations)
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402
import shutil  # noqa: E402
from datetime import timedelta  # noqa: E402

from homeassistant.setup import async_setup_component  # noqa: E402
from homeassistant.util import dt as dt_util  # noqa: E402
from pytest_homeassistant_custom_component.common import async_fire_time_changed  # noqa: E402

BLUEPRINTS = Path(__file__).parent.parent / "blueprints" / "automation" / DOMAIN


@pytest.fixture
async def blueprints(hass: HomeAssistant, setup):
    dest = Path(hass.config.path("blueprints", "automation", DOMAIN))
    dest.mkdir(parents=True, exist_ok=True)
    for f in BLUEPRINTS.glob("*.yaml"):
        shutil.copy(f, dest / f.name)
    yield
    shutil.rmtree(dest, ignore_errors=True)


async def _automation(hass, blueprint, inputs):
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "alias": blueprint,
                "use_blueprint": {"path": f"{DOMAIN}/{blueprint}", "input": inputs},
            }
        },
    )
    await hass.async_block_till_done()


async def _settle(hass):
    for _ in range(20):
        await asyncio.sleep(0)


async def _advance(hass, freezer, *, wait=True, **delta):
    await _settle(hass)  # let state changes register their timers first
    freezer.tick(timedelta(**delta))
    async_fire_time_changed(hass, dt_util.utcnow())
    if wait:
        await hass.async_block_till_done()
    else:  # an automation is sitting in a delay; don't wait for it to finish
        await _settle(hass)


async def test_blueprint_open_window(hass: HomeAssistant, blueprints, freezer):
    client = FakeClient.instance
    hass.states.async_set("binary_sensor.kitchen_window", "off")
    await _automation(
        hass,
        "open_window.yaml",
        {
            "windows": ["binary_sensor.kitchen_window"],
            "climate_entity": "climate.ptd",
            "open_delay": 3,
            "setback_temperature": 7,
        },
    )
    assert hass.states.get("climate.ptd").attributes["scheduled_temperature"] == 17.0

    hass.states.async_set("binary_sensor.kitchen_window", "on")
    await _advance(hass, freezer, minutes=1)
    assert ("target", 4, 7.0) not in client.calls  # not open long enough yet
    await _advance(hass, freezer, minutes=3)
    assert ("target", 4, 7.0) in client.calls

    hass.states.async_set("binary_sensor.kitchen_window", "off")
    await hass.async_block_till_done()
    assert client.calls[-1] == ("target", 4, 17.0)  # back to its scheduled temperature


async def test_blueprint_schedule_profile(hass: HomeAssistant, blueprints, freezer):
    client = FakeClient.instance
    hass.states.async_set("input_boolean.working_from_home", "off")
    wfh = {"weekdays": [{"start": "07:00", "temperature": 20}, {"start": "22:30", "temperature": 15}]}
    office = {"weekdays": [{"start": "06:00", "temperature": 19}, {"start": "08:00", "temperature": 14},
                           {"start": "17:30", "temperature": 20}, {"start": "22:30", "temperature": 15}]}
    await _automation(
        hass,
        "schedule_profile.yaml",
        {
            "switch_entity": "input_boolean.working_from_home",
            "zone_calendar": "calendar.downstairs_schedule",
            "schedule_on": wfh,
            "schedule_off": office,
        },
    )
    hass.states.async_set("input_boolean.working_from_home", "on")
    await _advance(hass, freezer, seconds=31)
    assert ("program_write", 1, 42) in client.calls
    stored = client.programs["1"][0]["D"]
    assert stored[0] == {"O": 420, "T": 200}
    assert stored[5] == {"O": 1350, "T": 150}
    assert stored[36:] == FIXTURE["programs"]["1"][0]["D"][36:]  # Sunday untouched

    hass.states.async_set("input_boolean.working_from_home", "off")
    await _advance(hass, freezer, seconds=31)
    assert client.programs["1"][0]["D"][:6] == [
        {"O": 360, "T": 190},
        {"O": 361, "T": 190},  # no-op repeats pad the day to 6 points
        {"O": 362, "T": 190},
        {"O": 480, "T": 140},
        {"O": 1050, "T": 200},
        {"O": 1350, "T": 150},
    ]


async def test_blueprint_away_when_empty(hass: HomeAssistant, blueprints, freezer):
    client = FakeClient.instance
    hass.states.async_set("person.a", "home")
    hass.states.async_set("person.b", "not_home")
    await _automation(
        hass,
        "away_when_empty.yaml",
        {"people": ["person.a", "person.b"], "climate_entity": "climate.downstairs", "leave_delay": 10},
    )
    hass.states.async_set("person.a", "not_home")
    await _advance(hass, freezer, wait=False, minutes=5)
    # A location update (attributes only) must not restart the wait
    hass.states.async_set("person.a", "not_home", {"latitude": 51.5})
    await _advance(hass, freezer, wait=False, minutes=4)
    assert ("preset", "away") not in client.calls  # 9 minutes: still waiting
    await _advance(hass, freezer, wait=False, minutes=2)
    assert ("preset", "away") in client.calls


async def test_set_schedule_write_timeout_but_stored_counts_as_success(
    hass: HomeAssistant, setup, monkeypatch
):
    from custom_components.securecontrols_thermostat import coordinator as coord
    from custom_components.securecontrols_thermostat.api import CannotConnect

    monkeypatch.setattr(coord, "WRITE_SETTLE_SECS", 0)
    client = FakeClient.instance
    client.write_error = CannotConnect("WebSocket response timed out after 15 seconds")
    result = await hass.services.async_call(
        DOMAIN,
        "set_schedule",
        {
            ATTR_ENTITY_ID: "calendar.ptd_schedule",
            "days": "sunday",
            "periods": [{"start": "07:30", "temperature": 21}, {"start": "23:00", "temperature": 15}],
        },
        blocking=True,
        return_response=True,
    )
    assert result["calendar.ptd_schedule"]["schedule"]["sunday"][0] == {
        "start": "07:30",
        "temperature": 21.0,
    }


async def test_set_schedule_write_timeout_and_not_stored_gives_clear_error(
    hass: HomeAssistant, setup, monkeypatch
):
    from custom_components.securecontrols_thermostat import coordinator as coord
    from custom_components.securecontrols_thermostat.api import CannotConnect

    monkeypatch.setattr(coord, "WRITE_SETTLE_SECS", 0)
    client = FakeClient.instance
    client.ignore_writes = True
    client.write_error = CannotConnect("WebSocket closed before the response arrived")
    with pytest.raises(HomeAssistantError, match="could not be sent .*closed before the response"):
        await hass.services.async_call(
            DOMAIN,
            "set_schedule",
            {
                ATTR_ENTITY_ID: "calendar.ptd_schedule",
                "days": "sunday",
                "periods": [{"start": "07:30", "temperature": 21}],
            },
            blocking=True,
        )


async def test_command_errors_are_readable(hass: HomeAssistant, setup):
    from custom_components.securecontrols_thermostat.api import CannotConnect

    async def offline(*_args, **_kw):
        raise CannotConnect("WebSocket response timed out after 15 seconds")

    FakeClient.instance.set_target_temp = offline
    with pytest.raises(HomeAssistantError, match="Upstairs: the command was not accepted"):
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {ATTR_ENTITY_ID: "climate.upstairs", "temperature": 20},
            blocking=True,
        )


async def test_calendar_attributes_feed_the_schedule_card(hass: HomeAssistant, setup):
    kitchen = hass.states.get("calendar.ptd_schedule").attributes
    assert (kitchen["zone"], kitchen["zone_type"]) == (4, "heating")
    assert kitchen["schedule"]["sunday"][0] == {"start": "07:30", "temperature": 20.0}
    hw = hass.states.get("calendar.hot_water_schedule").attributes
    assert hw["zone_type"] == "hot_water"
    assert hw["schedule"]["monday"][0] == {"start": "05:40", "state": "on"}


async def test_saved_schedule_shows_on_the_calendar_straight_away(hass: HomeAssistant, setup):
    periods = [{"start": "07:00", "temperature": 19}]
    await hass.services.async_call(
        DOMAIN,
        "set_schedule",
        {ATTR_ENTITY_ID: "calendar.ptd_schedule", "days": "sunday", "periods": periods},
        blocking=True,
        return_response=True,
    )
    await hass.async_block_till_done()
    sunday = hass.states.get("calendar.ptd_schedule").attributes["schedule"]["sunday"]
    assert sunday == [{"start": "07:00", "temperature": 19.0}]


async def test_schedule_card_is_served_and_loaded(hass: HomeAssistant, setup, monkeypatch):
    from custom_components.securecontrols_thermostat import frontend  # noqa: PLC0415

    registered, extra = [], []

    class FakeHttp:
        async def async_register_static_paths(self, configs):
            registered.extend(configs)

    hass.data.pop(frontend._REGISTERED, None)
    monkeypatch.setattr(hass, "http", FakeHttp(), raising=False)
    monkeypatch.setattr(
        "homeassistant.components.frontend.add_extra_js_url", lambda h, url: extra.append(url)
    )
    await frontend.async_register_card(hass)
    await frontend.async_register_card(hass)  # second entry / reload: no duplicate

    assert [c.url_path for c in registered] == [
        "/securecontrols_thermostat/securecontrols-schedule-card.js"
    ]
    assert Path(registered[0].path).is_file()
    assert len(extra) == 1 and extra[0].startswith(registered[0].url_path + "?v=")
