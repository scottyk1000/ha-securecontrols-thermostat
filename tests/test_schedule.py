"""Weekly program model, checked against the programs captured from a live H3747."""

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.securecontrols_thermostat import schedule as sched

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "h3747.json").read_text())
LONDON = ZoneInfo("Europe/London")
KINDS = {"1": "heating", "2": "heating", "3": "hot_water", "4": "heating"}


@pytest.mark.parametrize("zone", sorted(KINDS))
def test_wire_round_trip_is_exact(zone):
    raw = FIXTURE["programs"][zone][0]["D"]
    assert sched.build_wire(sched.parse_wire(raw), KINDS[zone]) == raw


@pytest.mark.parametrize("zone", sorted(KINDS))
def test_friendly_round_trip_keeps_behaviour(zone):
    kind = KINDS[zone]
    week = sched.parse_wire(FIXTURE["programs"][zone][0]["D"])
    friendly = sched.to_friendly(week, kind)
    rebuilt = sched.parse_wire(
        sched.build_wire({d: sched.from_friendly(friendly[d], kind) for d in sched.DAYS}, kind)
    )
    for day in range(21, 28):
        for minute in range(0, 1440, 5):
            at = datetime(2026, 9, day, minute // 60, minute % 60, tzinfo=LONDON)
            assert sched.value_at(rebuilt, at) == sched.value_at(week, at)


def test_friendly_output_drops_padding():
    week = sched.parse_wire(FIXTURE["programs"]["3"][0]["D"])
    assert sched.to_friendly(week, "hot_water")["monday"] == [
        {"start": "05:40", "state": "on"},
        {"start": "06:40", "state": "off"},
    ]


def test_heating_days_are_padded_to_six_no_op_points():
    wire = sched.build_wire(
        {d: [sched.Point(390, 200), sched.Point(1350, 150)] for d in sched.DAYS}, "heating"
    )
    assert wire[:6] == [
        {"O": 390, "T": 200},
        {"O": 391, "T": 200},
        {"O": 392, "T": 200},
        {"O": 393, "T": 200},
        {"O": 394, "T": 200},
        {"O": 1350, "T": 150},
    ]


def test_hot_water_days_use_unused_markers():
    wire = sched.build_wire(
        {d: [sched.Point(360, 1), sched.Point(420, 0)] for d in sched.DAYS}, "hot_water"
    )
    assert wire[2:6] == [{"O": 65535, "T": 65535}] * 4


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("weekdays", ["monday", "tuesday", "wednesday", "thursday", "friday"]),
        (["weekend", "mon"], ["saturday", "sunday", "monday"]),
        ("all", list(sched.DAYS)),
        (["Tue", "tuesday"], ["tuesday"]),
    ],
)
def test_expand_days(given, expected):
    assert sched.expand_days(given) == expected


def test_expand_days_rejects_unknown():
    with pytest.raises(sched.ScheduleError):
        sched.expand_days("funday")


def test_segments_carry_over_midnight_and_merge():
    week = sched.parse_wire(FIXTURE["programs"]["1"][0]["D"])
    segs = sched.segments(
        week,
        datetime(2026, 9, 21, 0, 0, tzinfo=LONDON),
        datetime(2026, 9, 22, 0, 0, tzinfo=LONDON),
    )
    assert [(a.strftime("%H:%M"), b.strftime("%H:%M"), v) for a, b, v in segs] == [
        ("00:00", "18:00", 120),  # Sunday night's 12 °C carries through the 06:00 repeat
        ("18:00", "22:00", 160),
        ("22:00", "00:00", 120),
    ]
