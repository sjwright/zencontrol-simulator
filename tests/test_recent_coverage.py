"""Coverage for features added in the recent simulator expansion.

Covers: QUERY_GROUP_BY_NUMBER, QUERY_SCENE_NUMBERS_BY_ADDRESS, fitting numbers,
QUERY_DALI_EAN, dim_time / fade progress ticks, and group-scene companion events.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from zencontrol_simulator.events import EventEmitter
from zencontrol_simulator.handlers import CMD, CommandDispatcher
from zencontrol_simulator.protocol import ParseFailure, ResponseType, checksum, parse_request
from zencontrol_simulator.server import Simulator
from zencontrol_simulator.world import FADE_PROGRESS_MIN_MS, load_world

CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"

zencontrol = pytest.importorskip("zencontrol")


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
# QUERY_SCENE_NUMBERS_BY_ADDRESS
# ---------------------------------------------------------------------------


def test_scene_numbers_by_address_lists_configured_scenes():
    disp, world, _ = _disp()
    # ECG 1: scenes 0,1,2
    req = parse_request(_basic(CMD["QUERY_SCENE_NUMBERS_BY_ADDRESS"], address=1))
    assert not isinstance(req, ParseFailure)
    resp = disp.handle(req)
    assert resp[0] == ResponseType.ANSWER
    assert list(resp[3:-1]) == [0, 1, 2]


def test_scene_numbers_by_address_level_zero_still_counts():
    """Scene level 0 is membership (under 0xFF), not 'absent'."""
    disp, world, _ = _disp()
    world.lights[1].scene_levels = [None] * 12
    world.lights[1].scene_levels[3] = 0
    req = parse_request(_basic(CMD["QUERY_SCENE_NUMBERS_BY_ADDRESS"], address=1))
    assert not isinstance(req, ParseFailure)
    assert list(disp.handle(req)[3:-1]) == [3]


@pytest.mark.asyncio
async def test_live_scene_numbers_by_address(live_protocol):
    p = live_protocol.protocol
    assert await p.query_scene_numbers_by_address(live_protocol.ecg(0)) == [0, 1, 2, 8, 9]
    # Switching gear has no scene levels
    assert await p.query_scene_numbers_by_address(live_protocol.ecg(10)) is None
    assert await p.query_scene_numbers_by_address(live_protocol.ecg(50)) is None


# ---------------------------------------------------------------------------
# QUERY_GROUP_BY_NUMBER
# ---------------------------------------------------------------------------


def test_group_by_number_empty_members_level_zero():
    disp, world, _ = _disp()
    # Detach all members from group 4
    for light in list(world.lights.values()):
        if 4 in light.groups:
            light.groups = [g for g in light.groups if g != 4]
    req = parse_request(_basic(CMD["QUERY_GROUP_BY_NUMBER"], address=4))
    assert not isinstance(req, ParseFailure)
    resp = disp.handle(req)
    assert resp[0] == ResponseType.ANSWER
    assert resp[3:6] == bytes([4, 0x01, 0x00])


def test_group_by_number_uses_visible_mid_fade(monkeypatch):
    import time as time_mod

    disp, world, _ = _disp()
    world.lights[4].set_level(0)
    world.lights[5].set_level(0)
    base = 2_000_000_000.0
    monkeypatch.setattr(time_mod, "time", lambda: base)
    world.lights[4].set_level(100, fading_seconds=10, fade_origin=66)
    world.lights[5].set_level(100, fading_seconds=10, fade_origin=66)
    monkeypatch.setattr(time_mod, "time", lambda: base + 5)
    req = parse_request(_basic(CMD["QUERY_GROUP_BY_NUMBER"], address=2))
    assert not isinstance(req, ParseFailure)
    resp = disp.handle(req)
    assert resp[0] == ResponseType.ANSWER
    assert resp[3] == 2 and resp[4] == 0x01
    assert 45 <= resp[5] <= 55  # mid-fade brightest ≈ 50


@pytest.mark.asyncio
async def test_live_group_by_number_hallway(live_protocol):
    p = live_protocol.protocol
    live_protocol.world.lights[4].set_level(30)
    live_protocol.world.lights[5].set_level(120)
    info = await p.query_group_by_number(live_protocol.group(2))
    assert info == (2, True, 120)


# ---------------------------------------------------------------------------
# Fitting numbers + EAN
# ---------------------------------------------------------------------------


def test_fitting_numbers_follow_yaml_controller_id(tmp_path):
    cfg = tmp_path / "fit.yaml"
    cfg.write_text(
        """
controller:
  mac: "02:00:00:00:00:01"
  label: "Fit"
  version: [2, 2, 11]
  fitting_number: "7"
lights:
  - address: 2
    label: "L"
    serial: 1
    level: 0
    cg_types: [6]
    groups: []
    scene_levels: [null, null, null, null, null, null, null, null, null, null, null, null]
groups: []
devices:
  - address: 4
    label: "Pad"
    serial: 2
    instances:
      - number: 2
        type: push_button
        label: "B2"
profiles:
  items: []
system_variables: []
"""
    )
    world = load_world(cfg)
    disp = CommandDispatcher(world, EventEmitter(world))
    assert world.fitting_number == "7"

    ctrl = parse_request(_basic(CMD["QUERY_CONTROLLER_FITTING_NUMBER"]))
    assert disp.handle(ctrl)[3:-1] == b"7"

    ecg = parse_request(_basic(CMD["QUERY_DALI_FITTING_NUMBER"], address=2))
    assert disp.handle(ecg)[3:-1] == b"7.2"

    ecd = parse_request(_basic(CMD["QUERY_DALI_FITTING_NUMBER"], address=64 + 4))
    assert disp.handle(ecd)[3:-1] == b"7.104"

    inst = parse_request(
        _basic(CMD["QUERY_DALI_INSTANCE_FITTING_NUMBER"], address=64 + 4, d2=2)
    )
    assert disp.handle(inst)[3:-1] == b"7.104.2"


def test_instance_fitting_rejects_ecg_address():
    disp, _, _ = _disp()
    req = parse_request(_basic(CMD["QUERY_DALI_INSTANCE_FITTING_NUMBER"], address=3, d2=0))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.NO_ANSWER


def test_ean_for_ecg_and_ecd():
    disp, _, _ = _disp()
    for addr in (0, 11, 64, 64 + 12):
        req = parse_request(_basic(CMD["QUERY_DALI_EAN"], address=addr))
        assert not isinstance(req, ParseFailure)
        resp = disp.handle(req)
        assert resp[0] == ResponseType.ANSWER
        assert int.from_bytes(resp[3:9], "big") == 10_000_000_000 + addr


@pytest.mark.asyncio
async def test_live_fitting_and_ean(live_protocol):
    p, c = live_protocol.protocol, live_protocol.controller
    assert await p.query_controller_fitting_number(c) == "1"
    assert await p.query_dali_fitting_number(live_protocol.ecg(7)) == "1.7"
    assert await p.query_dali_fitting_number(live_protocol.ecd(4)) == "1.104"
    assert await p.query_dali_instance_fitting_number(live_protocol.instance(4, 2)) == "1.104.2"
    assert await p.query_dali_ean(live_protocol.ecg(0)) == 10_000_000_000
    assert await p.query_dali_ean(live_protocol.ecd(0)) == 10_000_000_000 + 64
    assert await p.query_dali_ean(live_protocol.ecg(50)) is None


# ---------------------------------------------------------------------------
# Dim time + fade progress (scene and custom fade)
# ---------------------------------------------------------------------------


def test_scene_dim_time_starts_fade_with_origin(monkeypatch):
    import time as time_mod

    world = load_world(CONFIG)
    world.dim_time_ms = 3000
    events = EventEmitter(world)
    base = 2_100_000_000.0
    monkeypatch.setattr(time_mod, "time", lambda: base)
    events.apply_and_emit_scene(64, 1)  # group 0 — mixed destinations
    assert world.lights[0].fade_origin == 64
    assert world.lights[1].fade_origin == 64
    assert world.lights[0].fade_to == 80
    assert world.lights[1].fade_to == 100


def test_custom_fade_progress_and_completion(monkeypatch):
    import time as time_mod

    world = load_world(CONFIG)
    world.heartbeat_interval = 0
    sim = Simulator(world)
    base = 2_200_000_000.0
    monkeypatch.setattr(time_mod, "time", lambda: base)
    world.lights[1].set_level(0)
    world.apply_level(1, 100, fading_seconds=(FADE_PROGRESS_MIN_MS + 500) / 1000.0)

    monkeypatch.setattr(time_mod, "time", lambda: base + 1.0)
    mid = sim.tick_fade_progress()
    assert any(w == 1 and dest == 100 and cur < 100 for w, cur, dest in mid)

    monkeypatch.setattr(time_mod, "time", lambda: base + 5.0)
    done = sim.tick_fade_progress()
    assert (1, 100, 100) in done
    assert world.lights[1].fading_until is None


def test_group_custom_fade_progress_emits_group_wire(monkeypatch):
    import time as time_mod

    world = load_world(CONFIG)
    world.heartbeat_interval = 0
    sim = Simulator(world)
    base = 2_300_000_000.0
    monkeypatch.setattr(time_mod, "time", lambda: base)
    for addr in (4, 5):
        world.lights[addr].set_level(0)
    world.apply_level(66, 100, fading_seconds=3.0)  # group 2, >2s

    monkeypatch.setattr(time_mod, "time", lambda: base + 1.0)
    progress = sim.tick_fade_progress()
    member_wires = {w for w, _, _ in progress if w in (4, 5)}
    assert member_wires == {4, 5}
    assert any(w == 66 and dest == 100 for w, _, dest in progress)


@pytest.mark.asyncio
async def test_live_dim_time_scene_fades(live_protocol, monkeypatch):
    import time as time_mod

    p = live_protocol.protocol
    world = live_protocol.world
    world.dim_time_ms = FADE_PROGRESS_MIN_MS + 1000
    base = 2_400_000_000.0
    monkeypatch.setattr(time_mod, "time", lambda: base)

    assert await p.dali_scene(live_protocol.ecg(0), 1) is True
    assert world.lights[0].fading_until is not None
    assert world.lights[0].fade_to == 80

    monkeypatch.setattr(time_mod, "time", lambda: base + 1.5)
    progress = live_protocol.sim.tick_fade_progress()
    assert any(w == 0 and dest == 80 for w, _, dest in progress)


# ---------------------------------------------------------------------------
# Group scene companion events (scene + level + colour)
# ---------------------------------------------------------------------------


def test_group_scene_event_order_includes_colour(monkeypatch):
    disp, world, events = _disp()
    emitted = []
    monkeypatch.setattr(
        events,
        "emit",
        lambda t, c, p=b"", instance=None: emitted.append((t, int(c), bytes(p))) or True,
    )
    req = parse_request(_basic(CMD["DALI_SCENE"], address=65, d2=1))  # group 1
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK

    codes_by_target: dict[int, list[int]] = {}
    for t, c, _ in emitted:
        codes_by_target.setdefault(t, []).append(c)

    # Members + group all get SCENE_CHANGE
    for target in (1, 2, 65):
        assert 0x05 in codes_by_target[target]
    # RGB member + group get COLOUR_CHANGE
    assert 0x08 in codes_by_target[2]
    assert 0x08 in codes_by_target[65]
    # Mixed destinations → member LEVEL only
    assert 0x0B in codes_by_target[1] and 0x0B in codes_by_target[2]
    assert 0x0B not in codes_by_target.get(65, [])


@pytest.mark.asyncio
async def test_live_group_scene_colour_and_scene_events(live_protocol):
    p = live_protocol.protocol
    scenes: list = []
    colours: list = []

    async def on_scene(*, address, scene, active, payload):
        scenes.append((address.type.name, address.number, scene))

    async def on_colour(*, address, colour, payload):
        colours.append((address.type.name, address.number, colour))

    p.set_callbacks(scene_change_callback=on_scene, colour_change_callback=on_colour)
    await p.start_event_monitoring()
    await _wait(0.25)

    assert await p.dali_scene(live_protocol.group(1), 1) is True
    await _wait(0.4)

    assert any(t == "GROUP" and n == 1 and s == 1 for t, n, s in scenes)
    assert any(t == "ECG" and n == 1 and s == 1 for t, n, s in scenes)
    assert any(t == "ECG" and n == 2 and s == 1 for t, n, s in scenes)
    assert any(t == "ECG" and n == 2 for t, n, _ in colours)
    assert any(t == "GROUP" and n == 1 for t, n, _ in colours)
    assert live_protocol.world.lights[2].colour.b == 255


# ---------------------------------------------------------------------------
# Readiness stubs still honour YAML / runtime flags
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_readiness_stubs_toggle(live_protocol):
    p, c = live_protocol.protocol, live_protocol.controller
    assert await p.query_controller_startup_complete(c) is True
    assert await p.query_is_dali_ready(c) is True
    live_protocol.world.startup_complete = False
    assert await p.query_controller_startup_complete(c) is not True
    live_protocol.world.startup_complete = True
    live_protocol.world.dali_ready = False
    assert await p.query_is_dali_ready(c) is not True
