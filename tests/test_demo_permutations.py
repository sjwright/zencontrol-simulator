"""Permutation coverage for the expanded demo world in config.yaml.

Exercises entity kinds that exist in the demo config but were only lightly
touched by the core ECG 0–3 / group 0–1 suite: hallway overlap, switching
gear, second colour fixtures, ECD pad shapes, and extra system variables.
"""

import asyncio
from pathlib import Path

import pytest

from zencontrol_simulator.events import EventEmitter
from zencontrol_simulator.handlers import CMD, CommandDispatcher
from zencontrol_simulator.protocol import ParseFailure, ResponseType, checksum, parse_request
from zencontrol_simulator.world import load_world

CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"

zencontrol = pytest.importorskip("zencontrol")

from zencontrol import ZenColour, ZenColourType  # noqa: E402


def _basic(command: int, address: int = 0, d0: int = 0, d1: int = 0, d2: int = 0, seq: int = 1) -> bytes:
    packet = bytearray([0x04, seq, command, address, d0, d1, d2])
    packet.append(checksum(packet))
    return bytes(packet)


def _disp():
    world = load_world(CONFIG)
    events = EventEmitter(world)
    return CommandDispatcher(world, events), world, events


async def _wait(seconds: float = 0.15) -> None:
    await asyncio.sleep(seconds)


# ---------------------------------------------------------------------------
# State: hallway overlap (groups 2 ↔ 3 via light 5)
# ---------------------------------------------------------------------------


def test_hallway_group_scene_clears_sibling():
    """Light 5 is in groups 2 and 3 — scene on one clears the other."""
    disp, world, _ = _disp()
    # Group 3 scene first
    req = parse_request(_basic(CMD["DALI_SCENE"], address=67, d2=0))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    assert world.groups[3].last_scene_current is True
    assert world.lights[5].level == 200
    assert world.lights[6].level == 200

    # Group 2 scene shares light 5 → clears group 3
    req2 = parse_request(_basic(CMD["DALI_SCENE"], address=66, d2=1))
    assert not isinstance(req2, ParseFailure)
    assert disp.handle(req2)[0] == ResponseType.OK
    assert world.groups[2].last_scene_current is True
    assert world.groups[3].last_scene_current is False
    assert world.lights[4].level == 80
    assert world.lights[5].level == 80

    q = parse_request(_basic(CMD["DALI_QUERY_LAST_SCENE_IS_CURRENT"], address=67))
    assert not isinstance(q, ParseFailure)
    assert disp.handle(q)[3] == 0


def test_hallway_group_level_clears_sibling_scene():
    disp, world, _ = _disp()
    req = parse_request(_basic(CMD["DALI_SCENE"], address=66, d2=0))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    assert world.groups[2].last_scene_current is True

    req2 = parse_request(_basic(CMD["DALI_ARC_LEVEL"], address=67, d2=40))
    assert not isinstance(req2, ParseFailure)
    assert disp.handle(req2)[0] == ResponseType.OK
    assert world.groups[2].last_scene_current is False
    assert world.lights[5].level == 40
    assert world.lights[6].level == 40


# ---------------------------------------------------------------------------
# State: RGB / XY / second TC scene recall
# ---------------------------------------------------------------------------


def test_rgb_scene_recall_applies_colour():
    disp, world, _ = _disp()
    req = parse_request(_basic(CMD["DALI_SCENE"], address=2, d2=1))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    light = world.lights[2]
    assert light.level == 64
    assert light.last_scene == 1
    assert light.colour is not None
    assert light.colour.type == "rgbwaf"
    assert light.colour.r == 0 and light.colour.g == 0 and light.colour.b == 255


def test_xy_scene_recall_applies_colour():
    disp, world, _ = _disp()
    req = parse_request(_basic(CMD["DALI_SCENE"], address=3, d2=1))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    light = world.lights[3]
    assert light.level == 50
    assert light.colour is not None
    assert light.colour.type == "xy"
    assert light.colour.x == 15000 and light.colour.y == 18000


def test_second_tc_rgb_xy_identity_and_groups():
    world = load_world(CONFIG)
    assert world.lights[7].colour_features.supports_tunable
    assert world.lights[7].groups == [4]
    assert world.lights[7].colour.kelvin == 4000

    assert world.lights[8].colour_features.rgbwaf_channels == 3
    assert world.lights[8].groups == [4]
    assert world.lights[8].colour.r == 0 and world.lights[8].colour.b == 255

    assert world.lights[9].colour_features.supports_xy
    assert world.lights[9].groups == [4]
    assert world.lights[9].colour.x == 18000


def test_second_rgb_scene_recall():
    disp, world, _ = _disp()
    req = parse_request(_basic(CMD["DALI_SCENE"], address=8, d2=1))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    light = world.lights[8]
    assert light.level == 32
    assert light.colour.r == 255 and light.colour.g == 0 and light.colour.b == 128


# ---------------------------------------------------------------------------
# State: switching gear + group 5 / empty group scenes
# ---------------------------------------------------------------------------


def test_switching_gear_on_off_and_min_zero():
    disp, world, _ = _disp()
    for addr in (10, 11):
        light = world.lights[addr]
        assert 7 in light.cg_types
        assert light.min_level == 0
        assert light.level == 0

        on = parse_request(_basic(CMD["DALI_ARC_LEVEL"], address=addr, d2=254))
        assert not isinstance(on, ParseFailure)
        assert disp.handle(on)[0] == ResponseType.OK
        assert light.level == 254

        off = parse_request(_basic(CMD["DALI_OFF"], address=addr))
        assert not isinstance(off, ParseFailure)
        assert disp.handle(off)[0] == ResponseType.OK
        assert light.level == 0


def test_switching_gear_null_scenes_leave_level():
    disp, world, _ = _disp()
    world.lights[10].set_level(254)
    req = parse_request(_basic(CMD["DALI_SCENE"], address=10, d2=0))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    assert world.lights[10].level == 254  # no scene_levels configured
    assert world.lights[10].last_scene == 0
    assert world.lights[10].last_scene_current is True


def test_group_5_controls_both_switching_members():
    disp, world, _ = _disp()
    req = parse_request(_basic(CMD["DALI_ARC_LEVEL"], address=69, d2=254))  # group 5
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    assert world.lights[10].level == 254
    assert world.lights[11].level == 254
    assert world.group_level(5) == 254

    off = parse_request(_basic(CMD["DALI_OFF"], address=69))
    assert not isinstance(off, ParseFailure)
    assert disp.handle(off)[0] == ResponseType.OK
    assert world.lights[10].level == 0
    assert world.lights[11].level == 0


def test_groups_without_scenes_bitmask_empty():
    disp, world, _ = _disp()
    for group_num in (4, 5):
        assert world.groups[group_num].scenes == {}
        q = parse_request(_basic(CMD["QUERY_SCENE_NUMBERS_FOR_GROUP"], address=group_num))
        assert not isinstance(q, ParseFailure)
        resp = disp.handle(q)
        assert resp[0] == ResponseType.ANSWER
        assert resp[3:5] == b"\x00\x00"


def test_group_4_mixed_level_returns_255():
    disp, world, _ = _disp()
    world.lights[7].set_level(10)
    world.lights[8].set_level(20)
    world.lights[9].set_level(30)
    assert world.group_level(4) == 255
    q = parse_request(_basic(CMD["DALI_QUERY_LEVEL"], address=68))
    assert not isinstance(q, ParseFailure)
    assert disp.handle(q)[3] == 255


# ---------------------------------------------------------------------------
# State: ECD shapes + sysvars 2–5
# ---------------------------------------------------------------------------


def test_ecd_shape_inventory():
    world = load_world(CONFIG)

    def shape(addr: int) -> tuple[int, ...]:
        return tuple(sorted(i.type for i in world.devices[addr].instances))

    assert shape(1) == (1,)
    assert shape(3) == (1,)
    assert shape(2) == (1, 1, 1, 1, 1, 1)
    assert shape(4) == (1, 1, 1, 1, 1, 1)
    assert shape(5) == (1, 1, 1, 1, 1, 1, 6)
    assert shape(6) == (1, 1, 1, 1, 1, 1, 6)
    assert shape(7) == (1, 1, 1, 1)
    assert shape(8) == (1, 1, 1, 1)
    assert shape(9) == (1, 1, 1, 6)
    assert shape(12) == (1, 1, 1, 6)
    assert shape(10) == (3, 4)
    assert shape(11) == (3, 4)


def test_general_sensor_and_pad_labels():
    disp, world, _ = _disp()
    # ECD wire = 64 + address
    cases = [
        (64 + 5, 6, "Lounge GP"),
        (64 + 7, 0, "Master Up"),
        (64 + 9, 3, "Echo GP"),
        (64 + 10, 1, "Porch Lux"),
        (64 + 12, 0, "Annex B1"),
    ]
    for wire, inst, label in cases:
        q = parse_request(_basic(CMD["QUERY_DALI_INSTANCE_LABEL"], address=wire, d2=inst))
        assert not isinstance(q, ParseFailure)
        assert disp.handle(q)[3:-1] == label.encode()


def test_entrance_occupancy_hold_differs_from_porch():
    world = load_world(CONFIG)
    porch = world.instance(10, 0)
    entrance = world.instance(11, 0)
    assert porch is not None and porch.timers is not None
    assert entrance is not None and entrance.timers is not None
    assert porch.timers.hold == 60
    assert entrance.timers.hold == 90


def test_sysvars_extra_ids_and_negative_value():
    disp, world, _ = _disp()
    assert world.system_variables[2].name == "Garage Side Door switch"
    assert world.system_variables[3].name == "Porch Lux sensor"
    assert world.system_variables[4].name == "Garage Front Opening"
    assert world.system_variables[5].name == "Garage Front Closing"
    assert world.system_variables[5].value == -1
    assert world.system_variables[1].simulate == 2500
    for vid in (0, 2, 3, 4, 5):
        assert world.system_variables[vid].simulate is None

    q = parse_request(_basic(CMD["QUERY_SYSTEM_VARIABLE"], address=5))
    assert not isinstance(q, ParseFailure)
    resp = disp.handle(q)
    assert resp[0] == ResponseType.ANSWER
    assert int.from_bytes(resp[3:5], "big", signed=True) == -1

    set_req = parse_request(_basic(CMD["SET_SYSTEM_VARIABLE"], address=5, d1=0xFF, d2=0xFE))  # -2
    assert not isinstance(set_req, ParseFailure)
    assert disp.handle(set_req)[0] == ResponseType.OK
    assert world.system_variables[5].value == -2


def test_sysvar_simulation_leaves_non_simulate_ids():
    from zencontrol_simulator.server import Simulator

    world = load_world(CONFIG)
    world.bind_host = "127.0.0.1"
    world.bind_port = 0
    world.heartbeat_interval = 0
    before = {vid: var.value for vid, var in world.system_variables.items() if vid != 1}
    sim = Simulator(world)
    changed = sim.tick_sysvar_simulation(seconds_since_midnight=12 * 3600)
    assert all(vid == 1 for vid, _ in changed)
    for vid, value in before.items():
        assert world.system_variables[vid].value == value


# ---------------------------------------------------------------------------
# Live protocol: discovery + control permutations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_switching_gear_and_group_5(live_protocol):
    p = live_protocol.protocol
    for n in (10, 11):
        addr = live_protocol.ecg(n)
        cg = await p.dali_query_cg_type(addr)
        assert cg is not None and 7 in cg
        assert await p.dali_arc_level(addr, 254) is True
        assert await p.dali_query_level(addr) == 254
        assert await p.dali_off(addr) is True
        assert await p.dali_query_level(addr) == 0

    g5 = live_protocol.group(5)
    assert await p.query_group_label(g5) == "Utility"
    scenes = await p.query_scene_numbers_for_group(g5)
    assert scenes == [] or scenes is None or list(scenes) == []
    assert await p.dali_arc_level(g5, 254) is True
    assert live_protocol.world.lights[10].level == 254
    assert live_protocol.world.lights[11].level == 254


@pytest.mark.asyncio
async def test_live_hallway_overlap_and_empty_group_scenes(live_protocol):
    p = live_protocol.protocol
    g2 = live_protocol.group(2)
    g3 = live_protocol.group(3)
    g4 = live_protocol.group(4)

    assert await p.query_group_label(g2) == "Hallway North Wing"
    assert await p.query_group_label(g3) == "Hallway South Wing"
    assert await p.dali_scene(g2, 0) is True
    assert live_protocol.world.lights[4].level == 200
    assert live_protocol.world.lights[5].level == 200
    assert live_protocol.world.groups[2].last_scene_current is True

    assert await p.dali_arc_level(g3, 55) is True
    assert live_protocol.world.groups[2].last_scene_current is False
    assert live_protocol.world.lights[5].level == 55
    assert live_protocol.world.lights[6].level == 55

    assert await p.query_group_label(g4) == "All Lights"
    scenes4 = await p.query_scene_numbers_for_group(g4)
    assert scenes4 == [] or scenes4 is None or list(scenes4) == []


@pytest.mark.asyncio
async def test_live_rgb_and_xy_scene_recall(live_protocol):
    p = live_protocol.protocol
    rgb = live_protocol.ecg(2)
    assert await p.dali_scene(rgb, 1) is True
    assert await p.dali_query_level(rgb) == 64
    colour = await p.query_dali_colour(rgb)
    assert colour is not None
    assert colour.type == ZenColourType.RGBWAF
    assert colour.r == 0 and colour.g == 0 and colour.b == 255

    xy = live_protocol.ecg(3)
    assert await p.dali_scene(xy, 1) is True
    assert await p.dali_query_level(xy) == 50
    xy_colour = await p.query_dali_colour(xy)
    assert xy_colour is not None
    assert xy_colour.type == ZenColourType.XY
    assert xy_colour.x == 15000 and xy_colour.y == 18000


@pytest.mark.asyncio
async def test_live_second_colour_fixtures(live_protocol):
    p = live_protocol.protocol
    tc = live_protocol.ecg(7)
    assert await p.query_dali_device_label(tc) == "Office Desk"
    features = await p.query_dali_colour_features(tc)
    assert features is not None
    assert features.get("supports_tunable") or features.get("colour_temperature")
    colour = ZenColour(type=ZenColourType.TC, kelvin=5000)
    assert await p.dali_colour(tc, colour, level=120) is True
    queried = await p.query_dali_colour(tc)
    assert queried is not None and queried.kelvin == 5000
    assert await p.dali_query_level(tc) == 120

    rgb = live_protocol.ecg(8)
    assert await p.query_dali_device_label(rgb) == "RGB Cove"
    assert await p.dali_colour(
        rgb, ZenColour(type=ZenColourType.RGBWAF, r=1, g=2, b=3, w=0, a=0, f=0), level=40
    ) is True
    rgb_q = await p.query_dali_colour(rgb)
    assert rgb_q is not None and rgb_q.r == 1 and rgb_q.g == 2 and rgb_q.b == 3

    xy = live_protocol.ecg(9)
    assert await p.query_dali_device_label(xy) == "XY Niche"
    assert await p.dali_colour(xy, ZenColour(type=ZenColourType.XY, x=1111, y=2222)) is True
    xy_q = await p.query_dali_colour(xy)
    assert xy_q is not None and xy_q.x == 1111 and xy_q.y == 2222


@pytest.mark.asyncio
async def test_live_ecd_shape_discovery_and_labels(live_protocol):
    p = live_protocol.protocol
    devices = await p.query_dali_addresses_with_instances(live_protocol.controller, start_address=0)
    nums = {a.number for a in devices}
    assert {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13}.issubset(nums)

    bedhead = await p.query_instances_by_address(live_protocol.ecd(7))
    assert len(bedhead) == 4
    assert all(i.type.value == 1 for i in bedhead)

    echo = await p.query_instances_by_address(live_protocol.ecd(9))
    types = {(i.number, i.type.value) for i in echo}
    assert (0, 1) in types and (3, 6) in types

    lounge = await p.query_instances_by_address(live_protocol.ecd(5))
    assert any(i.type.value == 6 for i in lounge)

    absolute = await p.query_instances_by_address(live_protocol.ecd(13))
    assert len(absolute) == 1 and absolute[0].type.value == 0x02

    assert await p.query_dali_device_label(live_protocol.ecd(7)) == "Master Bedhead"
    assert await p.query_dali_instance_label(live_protocol.instance(5, 6, type_code=6)) == "Lounge GP"
    assert await p.query_dali_instance_label(live_protocol.instance(9, 3, type_code=6)) == "Echo GP"
    assert await p.query_dali_instance_label(live_protocol.instance(10, 1, type_code=4)) == "Porch Lux"
    assert await p.query_dali_instance_label(
        live_protocol.instance(13, 0, type_code=0x02)
    ) == "Slider"


@pytest.mark.asyncio
async def test_live_inject_on_alternate_ecds(live_protocol):
    p = live_protocol.protocol
    buttons: list = []
    occupied: list = []

    async def on_button(*, instance, payload):
        buttons.append((instance.address.number, instance.number))

    async def on_occ(*, instance, payload):
        occupied.append((instance.address.number, instance.number))

    p.set_callbacks(button_press_callback=on_button, is_occupied_callback=on_occ)
    await p.start_event_monitoring()
    await _wait(0.25)

    live_protocol.sim.inject_button_press(7, 0)  # Master Bedhead
    live_protocol.sim.inject_button_press(2, 5)  # Entrance 6-button
    live_protocol.sim.inject_occupancy(10, 0, occupied=True)  # Porch
    await _wait(0.4)

    assert any(ecd == 7 and inst == 0 for ecd, inst in buttons)
    assert any(ecd == 2 and inst == 5 for ecd, inst in buttons)
    assert any(ecd == 10 and inst == 0 for ecd, inst in occupied)


@pytest.mark.asyncio
async def test_live_sysvars_extra_and_profiles(live_protocol):
    p, c = live_protocol.protocol, live_protocol.controller
    assert await p.query_system_variable_name(c, 2) == "Garage Side Door switch"
    assert await p.query_system_variable_name(c, 3) == "Porch Lux sensor"
    assert await p.query_system_variable_name(c, 4) == "Garage Front Opening"
    assert await p.query_system_variable_name(c, 5) == "Garage Front Closing"
    assert await p.query_system_variable(c, 5) == -1
    assert await p.set_system_variable(c, 5, -3) is True
    assert await p.query_system_variable(c, 5) == -3
    assert await p.set_system_variable(c, 2, 1) is True
    assert await p.query_system_variable(c, 2) == 1

    assert await p.query_profile_label(c, 1) == "Day"
    assert await p.query_profile_label(c, 3) == "Away"
    assert await p.change_profile_number(c, 3) is True
    assert await p.query_current_profile_number(c) == 3


@pytest.mark.asyncio
async def test_live_entrance_occupancy_timers(live_protocol):
    p = live_protocol.protocol
    porch = await p.query_occupancy_instance_timers(live_protocol.instance(10, 0, type_code=3))
    entrance = await p.query_occupancy_instance_timers(live_protocol.instance(11, 0, type_code=3))
    assert porch is not None and porch["hold"] == 60
    assert entrance is not None and entrance["hold"] == 90
