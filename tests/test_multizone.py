# ruff: noqa: PLR2004
"""Multi-zone programmer (H3747 / C1727) support, using a real captured H3747 payload."""

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from custom_components.securecontrols_thermostat.api import (
    PROGRAM_WRITE_TIMEOUT_SECS,
    WS_RESPONSE_TIMEOUT_SECS,
    SecureControlsClient,
    ServerRejected,
)
from custom_components.securecontrols_thermostat.coordinator import (
    KIND_HEATING,
    KIND_HOT_WATER,
    ThermoCoordinator,
)
from tests.test_api import FakeWS, _authenticated_client, _patch_websockets

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "h3747.json").read_text())
LONDON = ZoneInfo("Europe/London")


def _coordinator(state=None, zones=None, programs=None) -> ThermoCoordinator:
    coordinator = object.__new__(ThermoCoordinator)
    coordinator.client = AsyncMock()
    coordinator.client.state_read.return_value = state or FIXTURE["state_read"]
    coordinator.client.zones_read.return_value = zones or FIXTURE["zones_read"]
    progs = programs or FIXTURE["programs"]
    coordinator.client.program_read.side_effect = lambda i: progs[str(i)]
    coordinator._state_cache = {}
    coordinator.topology = None
    coordinator._programs = {}
    coordinator._programs_read_at = None
    return coordinator


@pytest.mark.asyncio
async def test_h3747_zones_are_parsed_per_slot():
    coordinator = _coordinator()
    data = await coordinator._async_update_data()
    zones = data["zones"]

    assert sorted(zones) == [1, 2, 3, 4]
    assert {s: z["kind"] for s, z in zones.items()} == {
        1: KIND_HEATING,
        2: KIND_HEATING,
        3: KIND_HOT_WATER,
        4: KIND_HEATING,
    }
    assert zones[1]["name"] == "Downstairs"
    assert (zones[1]["target_c"], zones[1]["ambient_c"], zones[1]["humidity"]) == (12.0, 19.0, 71)
    # Zone 2 uses a sensor without a humidity probe (reports 255)
    assert (zones[2]["target_c"], zones[2]["ambient_c"], zones[2]["humidity"]) == (14.0, 23.0, None)
    assert (zones[4]["target_c"], zones[4]["ambient_c"], zones[4]["humidity"]) == (17.0, 18.0, 65)
    assert zones[1]["preset"] == "home"
    assert zones[2]["preset"] is None
    assert zones[1]["next_change_mins"] == 780  # 13:00, matches the zone 1 weekly program
    assert zones[3]["boost_active"] is False
    # Legacy top-level keys mirror the first heating zone
    assert data["target_c"] == 12.0
    assert data["ambient_c"] == 19.0
    # Every zone's weekly program is read (calendars + hot water state)
    assert [c.args[0] for c in coordinator.client.program_read.await_args_list] == [1, 2, 3, 4]
    assert zones[1]["schedule"]["monday"][0].minute == 360


@pytest.mark.asyncio
async def test_hot_water_boost_state_from_notify_values():
    state = json.loads(json.dumps(FIXTURE["state_read"]))
    hw = next(b for b in state["V"] if b["SI"] == 16)
    for item in hw["V"]:
        if item["I"] == 4:
            item.update({"V": 1, "OT": 2, "D": 60})
        if item["I"] == 9:
            item["V"] = 722
    coordinator = _coordinator(state=state)
    data = await coordinator._async_update_data()

    zone = data["zones"][3]
    assert zone["boost_active"] is True
    assert zone["boost_minutes"] == 60
    assert zone["is_on"] is True
    assert zone["next_change_mins"] == 722


@pytest.mark.asyncio
async def test_plain_thermostat_without_zones_read_still_works():
    coordinator = _coordinator(state={"V": [{"SI": 15, "V": [{"I": 1, "V": 215}]}]})
    coordinator.client.zones_read.side_effect = ServerRejected(-1, 4, {})
    data = await coordinator._async_update_data()

    assert data["target_c"] == 21.5
    assert list(data["zones"]) == [1]
    coordinator.client.program_read.assert_not_awaited()


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (datetime(2026, 9, 21, 5, 0, tzinfo=LONDON), False),  # Mon before 05:40
        (datetime(2026, 9, 21, 6, 0, tzinfo=LONDON), True),  # Mon 05:40-06:40 on
        (datetime(2026, 9, 21, 11, 0, tzinfo=LONDON), False),  # Mon late morning
        (datetime(2026, 9, 23, 6, 0, tzinfo=LONDON), False),  # Wed: morning slot is off
        (datetime(2026, 9, 27, 8, 0, tzinfo=LONDON), True),  # Sun 07:30-08:30 on
    ],
)
def test_hot_water_program_state(when, expected):
    program = FIXTURE["programs"]["3"][0]["D"]
    assert ThermoCoordinator.program_state_at(program, when) is expected


def test_next_change_is_minutes_since_midnight():
    now = datetime(2026, 9, 21, 11, 0, tzinfo=LONDON)
    assert ThermoCoordinator.minute_of_day_to_datetime(780, now) == datetime(
        2026, 9, 21, 13, 0, tzinfo=LONDON
    )
    # Already passed today -> tomorrow
    assert ThermoCoordinator.minute_of_day_to_datetime(340, now) == datetime(
        2026, 9, 22, 5, 40, tzinfo=LONDON
    )
    assert ThermoCoordinator.minute_of_day_to_datetime(None, now) is None


# ---------- write payloads ----------


async def _sent_body(session, monkeypatch, call):
    client = _authenticated_client(session)
    monkeypatch.setattr(SecureControlsClient, "_new_corr", lambda self: "42-write")
    websocket = FakeWS([{"I": "42-write", "R": 0}])
    _patch_websockets(monkeypatch, [websocket])
    await call(client)
    return websocket.sent[0]["P"]


@pytest.mark.asyncio
async def test_target_temperature_is_written_to_the_zone_slot(session, monkeypatch):
    header, args = await _sent_body(session, monkeypatch, lambda c: c.set_target_temp(21.0, slot=4))
    assert (header["HI"], header["SI"]) == (2, 15)
    assert args == [4, {"I": 1, "V": 210, "OT": 1, "D": 0}]


@pytest.mark.asyncio
async def test_hot_water_boost_payload(session, monkeypatch):
    header, args = await _sent_body(session, monkeypatch, lambda c: c.hot_water_boost(3, 60))
    assert (header["HI"], header["SI"]) == (2, 16)
    assert args == [3, {"I": 4, "V": 0, "OT": 2, "D": 60}]


@pytest.mark.asyncio
async def test_hot_water_cancel_boost_payload(session, monkeypatch):
    header, args = await _sent_body(session, monkeypatch, lambda c: c.hot_water_cancel_boost(3))
    assert (header["HI"], header["SI"]) == (2, 16)
    assert args == [3, {"I": 4, "V": 0, "OT": 2, "D": 0}]


@pytest.mark.asyncio
async def test_hot_water_boost_rejects_non_positive_duration(session):
    client = _authenticated_client(session)
    with pytest.raises(ValueError):
        await client.hot_water_boost(3, 0)


@pytest.mark.asyncio
async def test_program_write_waits_longer_than_normal_requests(session, monkeypatch):
    client = _authenticated_client(session)
    seen = {}

    async def fake_send(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(client, "_send_request", fake_send)
    await client.program_write(4, [{"O": 0, "T": 150}] * 42)
    assert seen["hi"] == 21 and seen["si"] == 17
    assert seen["timeout"] == PROGRAM_WRITE_TIMEOUT_SECS > WS_RESPONSE_TIMEOUT_SECS


@pytest.mark.asyncio
async def test_schedule_write_is_sent_compact_and_under_1kb(session, monkeypatch):
    """Programmers drop requests over ~1 KB; a weekly schedule only fits when compact."""
    client = _authenticated_client(session)
    monkeypatch.setattr(SecureControlsClient, "_new_corr", lambda self: "2127549-376258503")
    websocket = FakeWS([{"I": "2127549-376258503", "R": 0}])
    _patch_websockets(monkeypatch, [websocket])
    await client.program_write(4, FIXTURE["programs"]["4"][0]["D"])

    text = websocket.sent_text[0]
    assert ", " not in text and '": ' not in text
    assert len(text.encode()) < 1024
    assert json.loads(text)["P"][1] == [{"I": 4, "D": FIXTURE["programs"]["4"][0]["D"]}]
