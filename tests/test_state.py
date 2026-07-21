"""Additional tests for state mutation, labels, unicast, and event modes."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from zencontrol_simulator.events import EventEmitter
from zencontrol_simulator.handlers import CMD, CommandDispatcher
from zencontrol_simulator.protocol import (
    ErrorCode,
    ParseFailure,
    ResponseType,
    checksum,
    parse_request,
)
from zencontrol_simulator.world import load_world

CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"


def _basic(command: int, address: int = 0, d0: int = 0, d1: int = 0, d2: int = 0, seq: int = 1) -> bytes:
    packet = bytearray([0x04, seq, command, address, d0, d1, d2])
    packet.append(checksum(packet))
    return bytes(packet)


def _dynamic(command: int, data: bytes, seq: int = 1) -> bytes:
    packet = bytearray([0x04, seq, command, len(data)])
    packet.extend(data)
    packet.append(checksum(packet))
    return bytes(packet)


def _colour(address: int, level: int, colour: bytes, seq: int = 1) -> bytes:
    packet = bytearray([0x04, seq, 0x0E, address, level])
    packet.extend(colour)
    packet.append(checksum(packet))
    return bytes(packet)


def _disp():
    world = load_world(CONFIG)
    events = EventEmitter(world)
    return CommandDispatcher(world, events), world, events


def test_arc_mutates_level_and_query():
    disp, world, _ = _disp()
    before = world.lights[1].level
    assert before == 0
    req = parse_request(_basic(CMD["DALI_ARC_LEVEL"], address=1, d2=50))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    assert world.lights[1].level == 50

    q = parse_request(_basic(CMD["DALI_QUERY_LEVEL"], address=1))
    assert not isinstance(q, ParseFailure)
    resp = disp.handle(q)
    assert resp[3] == 50


def test_step_up_accumulates():
    disp, world, _ = _disp()
    world.lights[1].set_level(10)
    for _ in range(3):
        req = parse_request(_basic(CMD["DALI_UP"], address=1))
        assert not isinstance(req, ParseFailure)
        assert disp.handle(req)[0] == ResponseType.OK
    assert world.lights[1].level == 13


def test_scene_updates_last_scene_and_level():
    disp, world, _ = _disp()
    req = parse_request(_basic(CMD["DALI_SCENE"], address=0, d2=1))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    light = world.lights[0]
    assert light.last_scene == 1
    assert light.last_scene_current is True
    assert light.level == 80  # scene_levels[1]

    q = parse_request(_basic(CMD["DALI_QUERY_LAST_SCENE"], address=0))
    assert not isinstance(q, ParseFailure)
    assert disp.handle(q)[3] == 1


def test_colour_mutates_and_rejects_garbage():
    disp, world, _ = _disp()
    # TC 4000K
    req = parse_request(_colour(0, 0xFF, bytes([0x20, 0x0F, 0xA0])))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    assert world.lights[0].colour is not None
    assert world.lights[0].colour.kelvin == 4000

    bad = parse_request(_colour(0, 0xFF, bytes([0x01, 0x02])))
    assert not isinstance(bad, ParseFailure)
    assert disp.handle(bad)[0] == ResponseType.ERROR


def test_profile_and_sysvar_mutate():
    disp, world, _ = _disp()
    req = parse_request(_basic(CMD["CHANGE_PROFILE_NUMBER"], d1=0, d2=2))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    assert world.current_profile == 2

    req2 = parse_request(_basic(CMD["SET_SYSTEM_VARIABLE"], address=0, d1=0, d2=7))
    assert not isinstance(req2, ParseFailure)
    assert disp.handle(req2)[0] == ResponseType.OK
    assert world.system_variables[0].value == 7


def test_empty_label_is_no_answer(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        """
controller:
  mac: "02:00:00:00:00:01"
  label: "X"
  version: [2, 2, 11]
groups:
  - number: 0
    label: ""
lights: []
devices: []
profiles:
  items: []
system_variables: []
"""
    )
    world = load_world(cfg)
    disp = CommandDispatcher(world, EventEmitter(world))
    req = parse_request(_basic(CMD["QUERY_GROUP_LABEL"], address=0))
    assert not isinstance(req, ParseFailure)
    resp = disp.handle(req)
    assert resp[0] == ResponseType.NO_ANSWER


def test_unicast_dynamic_roundtrip():
    disp, world, _ = _disp()
    data = bytes([0x1B, 0x39, 192, 168, 1, 10])  # port 6969, 192.168.1.10
    req = parse_request(_dynamic(CMD["SET_TPI_EVENT_UNICAST_ADDRESS"], data))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    assert world.unicast_ip == "192.168.1.10"
    assert world.unicast_port == 6969

    q = parse_request(_basic(CMD["QUERY_TPI_EVENT_UNICAST_ADDRESS"]))
    assert not isinstance(q, ParseFailure)
    resp = disp.handle(q)
    assert resp[0] == ResponseType.ANSWER
    assert resp[3:-1][1:3] == bytes([0x1B, 0x39])
    assert list(resp[3:-1][3:7]) == [192, 168, 1, 10]


def test_event_mode_zero_suppresses(monkeypatch):
    disp, world, events = _disp()
    world.event_mode = 0x00
    sent = []
    monkeypatch.setattr(events, "_sock", type("S", (), {"sendto": lambda *a, **k: sent.append(a)})())
    # re-bind emit path - easier to just call emit
    assert events.emit(0, 0x0B, b"\x00\x01") is False
    assert events.sent_count == 0


def test_filter_mutes_level_v2(monkeypatch):
    disp, world, events = _disp()
    # enable + filtering
    world.event_mode = 0x03
    from zencontrol_simulator.world import EventFilter
    # mute LEVEL_CHANGE_V2 (bit 11)
    world.event_filters.append(EventFilter(address=0, instance=0xFF, mask=1 << 0x0B))
    assert events.emit(0, 0x0B, b"\x01\x02") is False


def test_go_to_last_active():
    disp, world, _ = _disp()
    world.lights[1].set_level(120)
    world.lights[1].set_level(0)
    assert world.lights[1].last_active_level == 120
    req = parse_request(_basic(CMD["DALI_GO_TO_LAST_ACTIVE_LEVEL"], address=1))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    assert world.lights[1].level == 120


def test_group_level_mixed_returns_255():
    disp, world, _ = _disp()
    world.lights[0].set_level(10)
    world.lights[1].set_level(20)  # both in group 0
    assert world.group_level(0) == 255
    req = parse_request(_basic(CMD["DALI_QUERY_LEVEL"], address=64))  # group 0 wire
    assert not isinstance(req, ParseFailure)
    resp = disp.handle(req)
    assert resp[3] == 255


def test_broadcast_scene_updates_groups():
    disp, world, _ = _disp()
    req = parse_request(_basic(CMD["DALI_SCENE"], address=255, d2=1))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    assert world.groups[0].last_scene == 1
    assert world.groups[0].last_scene_current is True
    assert world.lights[0].level == 80


def test_unknown_target_errors():
    disp, _, _ = _disp()
    req = parse_request(_basic(CMD["DALI_ARC_LEVEL"], address=40, d2=10))
    assert not isinstance(req, ParseFailure)
    resp = disp.handle(req)
    assert resp[0] == ResponseType.ERROR
    assert resp[3] == ErrorCode.UNKNOWN_TARGET


def test_occupy_resets_last_detect():
    from zencontrol_simulator.events import EventEmitter

    world = load_world(CONFIG)
    events = EventEmitter(world)
    inst = world.instance(0, 2)
    assert inst is not None and inst.timers is not None
    inst.timers.last_motion_at = time.time() - 99
    assert events.occupancy(0, 2, occupied=True) is True
    assert inst.timers.seconds_since_detect() <= 1


def test_heartbeat_does_not_note_motion():
    from zencontrol_simulator.events import EventEmitter

    world = load_world(CONFIG)
    events = EventEmitter(world)
    assert world.heartbeat_target() == (0, 2)
    inst = world.instance(0, 2)
    assert inst is not None and inst.timers is not None
    inst.timers.last_motion_at = time.time() - 99
    assert events.occupancy_heartbeat() is True
    assert inst.timers.seconds_since_detect() >= 90


@pytest.mark.asyncio
async def test_heartbeat_loop_emits():
    from zencontrol_simulator.server import Simulator

    world = load_world(CONFIG)
    world.bind_host = "127.0.0.1"
    world.bind_port = 0
    world.heartbeat_interval = 0.05
    sim = Simulator(world)
    await sim.start()
    before = sim.events.sent_count
    await asyncio.sleep(0.18)
    after = sim.events.sent_count
    await sim.stop()
    assert after >= before + 2


def test_occupancy_timer_query_advances(monkeypatch):
    import time as time_mod

    disp, world, _ = _disp()
    inst = world.instance(0, 2)
    assert inst is not None and inst.timers is not None
    base = 1_700_000_000.0
    inst.timers.last_motion_at = base
    monkeypatch.setattr(time_mod, "time", lambda: base + 45)

    req = parse_request(_basic(CMD["QUERY_OCCUPANCY_INSTANCE_TIMERS"], address=64, d2=2))
    assert not isinstance(req, ParseFailure)
    resp = disp.handle(req)
    assert resp[0] == ResponseType.ANSWER
    assert resp[3] == 1  # deadtime
    assert resp[4] == 60  # hold
    assert ((resp[6] << 8) | resp[7]) == 45


def test_occupancy_defaults_timers_without_config(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        """
controller:
  mac: "02:00:00:00:00:01"
  label: "X"
  version: [2, 2, 11]
groups: []
lights: []
devices:
  - address: 0
    label: "Sensor"
    instances:
      - number: 0
        type: occupancy_sensor
        label: "Motion"
profiles:
  items: []
system_variables: []
"""
    )
    world = load_world(cfg)
    inst = world.instance(0, 0)
    assert inst is not None
    assert inst.timers is not None
    disp = CommandDispatcher(world, EventEmitter(world))
    req = parse_request(_basic(CMD["QUERY_OCCUPANCY_INSTANCE_TIMERS"], address=64, d2=0))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.ANSWER


def test_group_level_emits_member_events(monkeypatch):
    disp, world, events = _disp()
    emitted = []
    monkeypatch.setattr(events, "emit", lambda t, c, p=b"", instance=None: emitted.append((t, int(c))) or True)
    req = parse_request(_basic(CMD["DALI_ARC_LEVEL"], address=64, d2=40))  # group 0
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    targets = {t for t, _ in emitted}
    assert 64 in targets  # group wire
    assert 0 in targets and 1 in targets  # members
    assert world.lights[0].level == 40
    assert world.lights[1].level == 40


def test_group_scene_emits_member_events(monkeypatch):
    disp, world, events = _disp()
    emitted = []
    monkeypatch.setattr(
        events, "emit", lambda t, c, p=b"", instance=None: emitted.append((t, int(c), p)) or True
    )
    req = parse_request(_basic(CMD["DALI_SCENE"], address=64, d2=1))  # group 0 scene 1
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    scene_targets = {t for t, c, _ in emitted if c == 0x05}
    assert 64 in scene_targets
    assert 0 in scene_targets and 1 in scene_targets
    assert world.lights[0].level == 80
    assert world.lights[1].level == 100


def test_unknown_profile_rejected():
    disp, world, _ = _disp()
    before = world.current_profile
    req = parse_request(_basic(CMD["CHANGE_PROFILE_NUMBER"], d1=0, d2=99))
    assert not isinstance(req, ParseFailure)
    resp = disp.handle(req)
    assert resp[0] == ResponseType.ERROR
    assert world.current_profile == before


def test_duplicate_light_address_rejected(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        """
controller:
  mac: "02:00:00:00:00:01"
  label: "X"
  version: [2, 2, 11]
groups: []
lights:
  - address: 0
    label: "A"
  - address: 0
    label: "B"
devices: []
profiles:
  items: []
system_variables: []
"""
    )
    with pytest.raises(ValueError, match="Duplicate light"):
        load_world(cfg)


def test_ecg_level_clears_parent_group_scene_current():
    disp, world, _ = _disp()
    # Put group into a scene-current state
    req = parse_request(_basic(CMD["DALI_SCENE"], address=64, d2=0))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    assert world.groups[0].last_scene_current is True

    req2 = parse_request(_basic(CMD["DALI_ARC_LEVEL"], address=0, d2=33))
    assert not isinstance(req2, ParseFailure)
    assert disp.handle(req2)[0] == ResponseType.OK
    assert world.groups[0].last_scene_current is False
    assert world.lights[0].level == 33

    q = parse_request(_basic(CMD["DALI_QUERY_LAST_SCENE_IS_CURRENT"], address=64))
    assert not isinstance(q, ParseFailure)
    assert disp.handle(q)[3] == 0


def test_group_scene_emits_group_colour_when_agreed(monkeypatch):
    disp, world, events = _disp()
    emitted = []
    monkeypatch.setattr(
        events, "emit", lambda t, c, p=b"", instance=None: emitted.append((t, int(c), p)) or True
    )
    # Group 1: lights 1 (no colour) + 2 (RGB with scene colours)
    req = parse_request(_basic(CMD["DALI_SCENE"], address=65, d2=1))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    group_colours = [p for t, c, p in emitted if t == 65 and c == 0x08]
    assert group_colours  # agreed RGB from ECG 2
    assert group_colours[0][0] == 0x80


def test_custom_fade_sets_status_bit_and_stop_clears():
    disp, world, _ = _disp()
    req = parse_request(_basic(CMD["DALI_CUSTOM_FADE"], address=1, d0=80, d1=0, d2=30))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    assert world.lights[1].level == 80  # destination
    assert world.lights[1].status & 0x10

    q = parse_request(_basic(CMD["DALI_QUERY_CONTROL_GEAR_STATUS"], address=1))
    assert not isinstance(q, ParseFailure)
    assert disp.handle(q)[3] & 0x10

    stop = parse_request(_basic(CMD["DALI_STOP_FADE"], address=1))
    assert not isinstance(stop, ParseFailure)
    assert disp.handle(stop)[0] == ResponseType.OK
    assert not (world.lights[1].status & 0x10)


def test_mid_fade_query_interpolates_and_stop_freezes(monkeypatch):
    import time as time_mod

    disp, world, _ = _disp()
    world.lights[1].set_level(0)
    base = 1_700_000_000.0
    monkeypatch.setattr(time_mod, "time", lambda: base)

    req = parse_request(_basic(CMD["DALI_CUSTOM_FADE"], address=1, d0=100, d1=0, d2=10))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK

    # Halfway through 10s fade 0→100
    monkeypatch.setattr(time_mod, "time", lambda: base + 5)
    q = parse_request(_basic(CMD["DALI_QUERY_LEVEL"], address=1))
    assert not isinstance(q, ParseFailure)
    mid = disp.handle(q)[3]
    assert 45 <= mid <= 55

    stop = parse_request(_basic(CMD["DALI_STOP_FADE"], address=1))
    assert not isinstance(stop, ParseFailure)
    assert disp.handle(stop)[0] == ResponseType.OK
    assert not (world.lights[1].status & 0x10)
    assert 45 <= world.lights[1].level <= 55

    # Stays frozen after more time
    monkeypatch.setattr(time_mod, "time", lambda: base + 20)
    q2 = parse_request(_basic(CMD["DALI_QUERY_LEVEL"], address=1))
    assert not isinstance(q2, ParseFailure)
    assert disp.handle(q2)[3] == world.lights[1].level


def test_inhibit_stores_duration():
    disp, world, _ = _disp()
    req = parse_request(_basic(CMD["DALI_INHIBIT"], address=1, d1=0, d2=10))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    assert world.lights[1].is_inhibited() is True

    clear = parse_request(_basic(CMD["DALI_INHIBIT"], address=1, d1=0, d2=0))
    assert not isinstance(clear, ParseFailure)
    assert disp.handle(clear)[0] == ResponseType.OK
    assert world.lights[1].is_inhibited() is False


def test_sysvar_set_rejects_unknown_id():
    disp, world, _ = _disp()
    req = parse_request(_basic(CMD["SET_SYSTEM_VARIABLE"], address=99, d1=0, d2=1))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.ERROR
    assert 99 not in world.system_variables


def test_event_filter_add_query_clear_roundtrip():
    disp, world, _ = _disp()
    mask = 1 << 0x0B  # mute LEVEL_CHANGE_V2
    add = parse_request(_basic(CMD["DALI_ADD_TPI_EVENT_FILTER"], address=0, d0=0xFF, d1=(mask >> 8), d2=(mask & 0xFF)))
    assert not isinstance(add, ParseFailure)
    assert disp.handle(add)[0] == ResponseType.OK

    q = parse_request(_basic(CMD["QUERY_DALI_TPI_EVENT_FILTERS"], address=0xFF, d0=0, d1=0, d2=0xFF))
    assert not isinstance(q, ParseFailure)
    resp = disp.handle(q)
    assert resp[0] == ResponseType.ANSWER
    # mode + one filter (addr, inst, mask_hi, mask_lo)
    assert resp[2] >= 5
    assert resp[4] == 0  # address
    assert ((resp[6] << 8) | resp[7]) & mask

    clear = parse_request(
        _basic(CMD["DALI_CLEAR_TPI_EVENT_FILTERS"], address=0, d0=0xFF, d1=(mask >> 8), d2=(mask & 0xFF))
    )
    assert not isinstance(clear, ParseFailure)
    assert disp.handle(clear)[0] == ResponseType.OK
    assert world.event_filters == []

    empty = parse_request(_basic(CMD["QUERY_DALI_TPI_EVENT_FILTERS"], address=0xFF, d0=0, d1=0, d2=0xFF))
    assert not isinstance(empty, ParseFailure)
    assert disp.handle(empty)[0] == ResponseType.NO_ANSWER

    # PDF: clearing a non-existent filter → NO_ANSWER
    clear_again = parse_request(
        _basic(CMD["DALI_CLEAR_TPI_EVENT_FILTERS"], address=0, d0=0xFF, d1=(mask >> 8), d2=(mask & 0xFF))
    )
    assert not isinstance(clear_again, ParseFailure)
    assert disp.handle(clear_again)[0] == ResponseType.NO_ANSWER


def test_group_colour_clears_scene_current():
    disp, world, _ = _disp()
    req = parse_request(_basic(CMD["DALI_SCENE"], address=64, d2=0))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    assert world.groups[0].last_scene_current is True

    # TC colour on group 0
    packet = bytearray([0x04, 1, CMD["DALI_COLOUR"], 64, 0xFF, 0x20, 0x0F, 0xA0])
    packet.append(checksum(packet))
    colour_req = parse_request(bytes(packet))
    assert not isinstance(colour_req, ParseFailure)
    assert disp.handle(colour_req)[0] == ResponseType.OK
    assert world.groups[0].last_scene_current is False


def test_ecg_scene_clears_parent_group_scene_current():
    disp, world, _ = _disp()
    req = parse_request(_basic(CMD["DALI_SCENE"], address=64, d2=0))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    assert world.groups[0].last_scene_current is True

    req2 = parse_request(_basic(CMD["DALI_SCENE"], address=0, d2=1))
    assert not isinstance(req2, ParseFailure)
    assert disp.handle(req2)[0] == ResponseType.OK
    assert world.lights[0].last_scene_current is True
    assert world.groups[0].last_scene_current is False


def test_group_scene_clears_sibling_group_scene_current():
    """Light 1 is in groups 0 and 1 — controlling group 0 must clear group 1."""
    disp, world, _ = _disp()
    # Scene on group 1 first
    req = parse_request(_basic(CMD["DALI_SCENE"], address=65, d2=0))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    assert world.groups[1].last_scene_current is True

    # Scene on group 0 shares light 1 → clears sibling group 1
    req2 = parse_request(_basic(CMD["DALI_SCENE"], address=64, d2=1))
    assert not isinstance(req2, ParseFailure)
    assert disp.handle(req2)[0] == ResponseType.OK
    assert world.groups[0].last_scene_current is True
    assert world.groups[1].last_scene_current is False

    q = parse_request(_basic(CMD["DALI_QUERY_LAST_SCENE_IS_CURRENT"], address=65))
    assert not isinstance(q, ParseFailure)
    assert disp.handle(q)[3] == 0


def test_group_level_clears_sibling_group_scene_current():
    disp, world, _ = _disp()
    req = parse_request(_basic(CMD["DALI_SCENE"], address=65, d2=0))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    assert world.groups[1].last_scene_current is True

    # Arc on group 0 shares light 1 with group 1
    req2 = parse_request(_basic(CMD["DALI_ARC_LEVEL"], address=64, d2=40))
    assert not isinstance(req2, ParseFailure)
    assert disp.handle(req2)[0] == ResponseType.OK
    assert world.groups[1].last_scene_current is False


def test_group_go_last_active_is_per_member():
    disp, world, _ = _disp()
    world.lights[0].set_level(100)
    world.lights[0].set_level(0)
    world.lights[1].set_level(50)
    world.lights[1].set_level(0)
    assert world.lights[0].last_active_level == 100
    assert world.lights[1].last_active_level == 50

    req = parse_request(_basic(CMD["DALI_GO_TO_LAST_ACTIVE_LEVEL"], address=64))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    assert world.lights[0].level == 100
    assert world.lights[1].level == 50


def test_wire_127_is_not_broadcast():
    """PDF allows broadcast 127 or 255, but 127 collides with ECD 63.

    Advanced clients (zencontrol-python) use 255 only; simulator keeps that
    convention so ECD address 63 remains addressable.
    """
    disp, _, _ = _disp()
    req = parse_request(_basic(CMD["DALI_ARC_LEVEL"], address=127, d2=10))
    assert not isinstance(req, ParseFailure)
    resp = disp.handle(req)
    assert resp[0] == ResponseType.ERROR
    assert resp[3] == ErrorCode.UNKNOWN_TARGET


def test_instance_filter_does_not_mute_gear_events():
    from zencontrol_simulator.world import EventFilter

    disp, world, events = _disp()
    world.event_mode = 0x03  # enabled + filtering
    # ECD 0 instance 0 mute of LEVEL_CHANGE_V2 must not affect group wire 64 gear events
    world.event_filters.append(EventFilter(address=64, instance=0, mask=1 << 0x0B))
    assert events.emit(64, 0x0B, b"\x01\x02") is True  # gear/group event, no instance

    # Same address+instance filter with button-press bit does mute button events
    world.event_filters[0].mask |= 1 << 0x00
    assert events.emit(64, 0x00, b"\x00", instance=0) is False


def test_broadcast_step_is_relative_per_light():
    disp, world, _ = _disp()
    world.lights[0].set_level(100)
    world.lights[1].set_level(50)
    world.lights[2].set_level(10)
    req = parse_request(_basic(CMD["DALI_UP"], address=255))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    assert world.lights[0].level == 101
    assert world.lights[1].level == 51
    assert world.lights[2].level == 11


def test_step_respects_max_level():
    disp, world, _ = _disp()
    world.lights[1].max_level = 50
    world.lights[1].set_level(50)
    req = parse_request(_basic(CMD["DALI_UP"], address=1))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    assert world.lights[1].level == 50


def test_scene_without_colour_does_not_emit_colour(monkeypatch):
    disp, world, events = _disp()
    # ECG 0 scene 2 has level 0 but null colour in config
    emitted = []
    monkeypatch.setattr(
        events, "emit", lambda t, c, p=b"", instance=None: emitted.append((t, int(c))) or True
    )
    req = parse_request(_basic(CMD["DALI_SCENE"], address=0, d2=2))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    assert all(c != 0x08 for _, c in emitted)


def test_apply_colour_copies_per_light():
    disp, world, _ = _disp()
    packet = bytearray([0x04, 1, CMD["DALI_COLOUR"], 255, 0xFF, 0x20, 0x0F, 0xA0])
    packet.append(checksum(packet))
    req = parse_request(bytes(packet))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    assert world.lights[0].colour is not None
    assert world.lights[2].colour is not None
    assert world.lights[0].colour is not world.lights[2].colour
    world.lights[0].colour.kelvin = 1234
    assert world.lights[2].colour.kelvin == 4000


def test_dali_up_does_not_ignite():
    disp, world, _ = _disp()
    world.lights[1].set_level(0)
    req = parse_request(_basic(CMD["DALI_UP"], address=1))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    assert world.lights[1].level == 0


def test_dali_on_step_up_ignites():
    disp, world, _ = _disp()
    world.lights[1].set_level(0)
    req = parse_request(_basic(CMD["DALI_ON_STEP_UP"], address=1))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    assert world.lights[1].level == world.lights[1].min_level


def test_dali_down_stays_at_min():
    disp, world, _ = _disp()
    world.lights[1].min_level = 1
    world.lights[1].set_level(1)
    req = parse_request(_basic(CMD["DALI_DOWN"], address=1))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    assert world.lights[1].level == 1


def test_dali_step_down_off_extinguishes_at_min():
    disp, world, _ = _disp()
    world.lights[1].min_level = 1
    world.lights[1].set_level(1)
    req = parse_request(_basic(CMD["DALI_STEP_DOWN_OFF"], address=1))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    assert world.lights[1].level == 0


def test_fade_to_off_status_follows_visible_until_stop(monkeypatch):
    import time as time_mod

    disp, world, _ = _disp()
    world.lights[1].set_level(100)
    base = 1_700_000_000.0
    monkeypatch.setattr(time_mod, "time", lambda: base)
    req = parse_request(_basic(CMD["DALI_CUSTOM_FADE"], address=1, d0=0, d1=0, d2=10))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK

    monkeypatch.setattr(time_mod, "time", lambda: base + 5)
    status = parse_request(_basic(CMD["DALI_QUERY_CONTROL_GEAR_STATUS"], address=1))
    assert not isinstance(status, ParseFailure)
    assert disp.handle(status)[3] & 0x04  # still visibly on mid-fade

    stop = parse_request(_basic(CMD["DALI_STOP_FADE"], address=1))
    assert not isinstance(stop, ParseFailure)
    assert disp.handle(stop)[0] == ResponseType.OK
    assert world.lights[1].level > 0
    assert world.lights[1].status & 0x04


def test_stop_fade_respects_origin_wire(monkeypatch):
    import time as time_mod

    disp, world, _ = _disp()
    world.lights[0].set_level(100)
    world.lights[1].set_level(100)
    base = 1_700_000_000.0
    monkeypatch.setattr(time_mod, "time", lambda: base)
    # Group 0 custom fade
    req = parse_request(_basic(CMD["DALI_CUSTOM_FADE"], address=64, d0=0, d1=0, d2=10))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    assert world.lights[0].fade_origin == 64

    # ECG stop must not kill a group-started fade
    stop = parse_request(_basic(CMD["DALI_STOP_FADE"], address=0))
    assert not isinstance(stop, ParseFailure)
    assert disp.handle(stop)[0] == ResponseType.OK
    assert world.lights[0].status & 0x10

    # Group stop does
    stop_g = parse_request(_basic(CMD["DALI_STOP_FADE"], address=64))
    assert not isinstance(stop_g, ParseFailure)
    assert disp.handle(stop_g)[0] == ResponseType.OK
    assert not (world.lights[0].status & 0x10)


def test_broadcast_colour_emits_group_targets(monkeypatch):
    disp, world, events = _disp()
    emitted = []
    monkeypatch.setattr(
        events, "emit", lambda t, c, p=b"", instance=None: emitted.append((t, int(c))) or True
    )
    packet = bytearray([0x04, 1, CMD["DALI_COLOUR"], 255, 0xFF, 0x20, 0x0F, 0xA0])
    packet.append(checksum(packet))
    req = parse_request(bytes(packet))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    colour_targets = {t for t, c in emitted if c == 0x08}
    assert 0 in colour_targets and 2 in colour_targets
    assert 64 in colour_targets and 65 in colour_targets


def test_mixed_recall_max_omits_group_level_event(monkeypatch):
    disp, world, events = _disp()
    world.lights[0].max_level = 100
    world.lights[1].max_level = 50
    emitted = []
    monkeypatch.setattr(
        events, "emit", lambda t, c, p=b"", instance=None: emitted.append((t, int(c), p)) or True
    )
    req = parse_request(_basic(CMD["DALI_RECALL_MAX"], address=64))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    assert world.lights[0].level == 100
    assert world.lights[1].level == 50
    assert world.group_level(0) == 255
    group_levels = [p for t, c, p in emitted if t == 64 and c == 0x0B]
    assert group_levels == []


# ---------------------------------------------------------------------------
# Additional coverage: inhibit expiry, fade complete, readiness, inject, etc.
# ---------------------------------------------------------------------------


def test_inhibit_expires_with_wall_clock(monkeypatch):
    import time as time_mod

    disp, world, _ = _disp()
    base = 1_700_000_000.0
    monkeypatch.setattr(time_mod, "time", lambda: base)
    req = parse_request(_basic(CMD["DALI_INHIBIT"], address=1, d1=0, d2=5))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    assert world.lights[1].is_inhibited() is True

    monkeypatch.setattr(time_mod, "time", lambda: base + 6)
    assert world.lights[1].is_inhibited() is False


def test_group_and_broadcast_inhibit():
    disp, world, _ = _disp()
    req = parse_request(_basic(CMD["DALI_INHIBIT"], address=64, d1=0, d2=30))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    assert world.groups[0].is_inhibited() is True
    assert world.lights[0].is_inhibited() is True
    assert world.lights[1].is_inhibited() is True

    clear = parse_request(_basic(CMD["DALI_INHIBIT"], address=255, d1=0, d2=0))
    assert not isinstance(clear, ParseFailure)
    assert disp.handle(clear)[0] == ResponseType.OK
    assert world.groups[0].is_inhibited() is False
    assert all(not lt.is_inhibited() for lt in world.lights.values())


def test_fade_auto_completes_without_stop(monkeypatch):
    import time as time_mod

    disp, world, _ = _disp()
    world.lights[1].set_level(0)
    base = 1_700_000_000.0
    monkeypatch.setattr(time_mod, "time", lambda: base)
    req = parse_request(_basic(CMD["DALI_CUSTOM_FADE"], address=1, d0=90, d1=0, d2=4))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    assert world.lights[1].status & 0x10

    monkeypatch.setattr(time_mod, "time", lambda: base + 5)
    q = parse_request(_basic(CMD["DALI_QUERY_LEVEL"], address=1))
    assert not isinstance(q, ParseFailure)
    assert disp.handle(q)[3] == 90
    assert not (world.lights[1].status & 0x10)
    status = parse_request(_basic(CMD["DALI_QUERY_CONTROL_GEAR_STATUS"], address=1))
    assert not isinstance(status, ParseFailure)
    assert not (disp.handle(status)[3] & 0x10)


def test_group_mid_fade_query_level(monkeypatch):
    import time as time_mod

    disp, world, _ = _disp()
    world.lights[0].set_level(0)
    world.lights[1].set_level(0)
    base = 1_700_000_000.0
    monkeypatch.setattr(time_mod, "time", lambda: base)
    req = parse_request(_basic(CMD["DALI_CUSTOM_FADE"], address=64, d0=100, d1=0, d2=10))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK

    monkeypatch.setattr(time_mod, "time", lambda: base + 5)
    q = parse_request(_basic(CMD["DALI_QUERY_LEVEL"], address=64))
    assert not isinstance(q, ParseFailure)
    mid = disp.handle(q)[3]
    assert 45 <= mid <= 55


def test_empty_colour_scene_membership_no_answer():
    disp, _, _ = _disp()
    # Dimmer ECG 1 has no scene colours
    req = parse_request(_basic(CMD["QUERY_COLOUR_SCENE_MEMBERSHIP_BY_ADDR"], address=1))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.NO_ANSWER


def test_query_unknown_ecg_no_answer():
    disp, _, _ = _disp()
    for cmd in (
        CMD["DALI_QUERY_LEVEL"],
        CMD["QUERY_DALI_DEVICE_LABEL"],
        CMD["QUERY_DALI_COLOUR"],
        CMD["DALI_QUERY_CONTROL_GEAR_STATUS"],
    ):
        req = parse_request(_basic(cmd, address=50))
        assert not isinstance(req, ParseFailure)
        assert disp.handle(req)[0] == ResponseType.NO_ANSWER


def test_query_unknown_group_no_answer():
    disp, _, _ = _disp()
    req = parse_request(_basic(CMD["QUERY_GROUP_LABEL"], address=15))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.NO_ANSWER


def test_colour_scene_8_11_data():
    disp, _, _ = _disp()
    req = parse_request(_basic(CMD["QUERY_COLOUR_SCENE_8_11_DATA_FOR_ADDR"], address=0))
    assert not isinstance(req, ParseFailure)
    resp = disp.handle(req)
    assert resp[0] == ResponseType.ANSWER
    assert resp[2] == 28
    blob = resp[3:-1]
    assert blob[0] == 0x20
    assert (blob[1] << 8) | blob[2] == 4500
    assert blob[7] == 0x20
    assert (blob[8] << 8) | blob[9] == 5500


def test_instance_and_ecd_labels():
    disp, _, _ = _disp()
    req = parse_request(_basic(CMD["QUERY_DALI_INSTANCE_LABEL"], address=64, d2=2))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[3:-1] == b"Motion"

    req2 = parse_request(_basic(CMD["QUERY_DALI_DEVICE_LABEL"], address=65))
    assert not isinstance(req2, ParseFailure)
    assert disp.handle(req2)[3:-1] == b"Kitchen Switch"

    missing = parse_request(_basic(CMD["QUERY_DALI_INSTANCE_LABEL"], address=64, d2=9))
    assert not isinstance(missing, ParseFailure)
    assert disp.handle(missing)[0] == ResponseType.NO_ANSWER


def test_xy_colour_set_and_query():
    disp, world, _ = _disp()
    colour = bytes([0x10, 0x4E, 0x20, 0x55, 0xF0])  # x=20000, y=22000
    packet = bytearray([0x04, 1, CMD["DALI_COLOUR"], 3, 128])
    packet.extend(colour)
    packet.append(checksum(packet))
    req = parse_request(bytes(packet))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK
    assert world.lights[3].colour.x == 20000
    assert world.lights[3].level == 128

    q = parse_request(_basic(CMD["QUERY_DALI_COLOUR"], address=3))
    assert not isinstance(q, ParseFailure)
    resp = disp.handle(q)
    assert resp[3] == 0x10
    assert (resp[4] << 8) | resp[5] == 20000


def test_startup_dali_ready_false_no_answer():
    disp, world, _ = _disp()
    world.startup_complete = False
    assert disp.handle(parse_request(_basic(CMD["QUERY_CONTROLLER_STARTUP_COMPLETE"])))[0] == ResponseType.NO_ANSWER
    world.startup_complete = True
    world.dali_ready = False
    assert disp.handle(parse_request(_basic(CMD["QUERY_IS_DALI_READY"])))[0] == ResponseType.NO_ANSWER


def test_inject_level_scene_colour_profile(monkeypatch):
    from zencontrol_simulator.server import Simulator
    from zencontrol_simulator.world import Colour

    world = load_world(CONFIG)
    sim = Simulator(world)
    emitted = []
    monkeypatch.setattr(
        sim.events, "emit", lambda t, c, p=b"", instance=None: emitted.append((t, int(c), p)) or True
    )

    sim.inject_level(1, 66)
    assert world.lights[1].level == 66
    assert any(t == 1 and c == 0x0B for t, c, _ in emitted)

    sim.inject_scene(0, 1)
    assert world.lights[0].last_scene == 1
    assert any(t == 0 and c == 0x05 for t, c, _ in emitted)

    sim.inject_colour(3, Colour(type="xy", x=1000, y=2000))
    assert world.lights[3].colour.x == 1000
    assert any(t == 3 and c == 0x08 for t, c, _ in emitted)

    sim.inject_profile(2)
    assert world.current_profile == 2
    assert any(c == 0x09 for _, c, _ in emitted)


def test_config_validation_warnings(tmp_path, caplog):
    import logging

    cfg = tmp_path / "bad.yaml"
    cfg.write_text(
        """
controller:
  mac: "02:00:00:00:00:01"
  label: "X"
  version: [2, 2, 11]
groups: []
lights:
  - address: 0
    label: "Bad TW"
    cg_types: [6]
    colour_features:
      supports_tunable: true
  - address: 1
    label: "Bad RGB"
    cg_types: [6]
    colour_features:
      rgbwaf_channels: 9
  - address: 2
    label: "Missing group"
    groups: [7]
devices: []
profiles:
  current: 99
  items: []
system_variables:
  - id: 0
    name: "A"
    value: 0
  - id: 20
    name: "Far"
    value: 1
"""
    )
    with caplog.at_level(logging.WARNING):
        load_world(cfg)
    text = " ".join(r.message for r in caplog.records)
    assert "supports_tunable" in text
    assert "rgbwaf_channels" in text
    assert "missing group" in text.lower() or "references missing group" in text
    assert "current profile" in text.lower() or "not in profiles" in text
    assert "gap" in text.lower() or "System variable gap" in text


def test_server_replies_error_on_bad_checksum():
    """UDP path returns ERROR for ParseFailure (checksum)."""
    import asyncio
    import socket

    from zencontrol_simulator.server import Simulator

    async def _run():
        world = load_world(CONFIG)
        world.bind_host = "127.0.0.1"
        world.bind_port = 0
        world.heartbeat_interval = 0
        sim = Simulator(world)
        await sim.start()
        port = sim._transport.get_extra_info("sockname")[1]
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(1.0)
        loop = asyncio.get_running_loop()
        try:
            packet = bytearray(_basic(CMD["QUERY_CONTROLLER_LABEL"]))
            packet[-1] ^= 0xFF
            sock.sendto(bytes(packet), ("127.0.0.1", port))
            # Yield so the asyncio UDP server can handle the datagram and reply
            await asyncio.sleep(0.05)
            data, _ = await loop.run_in_executor(None, sock.recvfrom, 256)
            assert data[0] == ResponseType.ERROR
            assert data[3] == ErrorCode.CHECKSUM
        finally:
            sock.close()
            await sim.stop()

    asyncio.run(_run())


def test_group_status_aggregates_fade(monkeypatch):
    import time as time_mod

    disp, world, _ = _disp()
    world.lights[0].set_level(0)
    base = 1_700_000_000.0
    monkeypatch.setattr(time_mod, "time", lambda: base)
    req = parse_request(_basic(CMD["DALI_CUSTOM_FADE"], address=0, d0=100, d1=0, d2=10))
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK

    q = parse_request(_basic(CMD["DALI_QUERY_CONTROL_GEAR_STATUS"], address=64))
    assert not isinstance(q, ParseFailure)
    assert disp.handle(q)[3] & 0x10


def test_scene_levels_include_high_scenes():
    disp, _, _ = _disp()
    req = parse_request(_basic(CMD["QUERY_SCENE_LEVELS_BY_ADDRESS"], address=0))
    assert not isinstance(req, ParseFailure)
    resp = disp.handle(req)
    levels = list(resp[3:-1])
    assert levels[8] == 160
    assert levels[9] == 40


def test_colour_membership_includes_high_scenes():
    disp, _, _ = _disp()
    req = parse_request(_basic(CMD["QUERY_COLOUR_SCENE_MEMBERSHIP_BY_ADDR"], address=0))
    assert not isinstance(req, ParseFailure)
    resp = disp.handle(req)
    assert resp[0] == ResponseType.ANSWER
    scenes = set(resp[3:-1])
    assert 0 in scenes and 1 in scenes and 8 in scenes and 9 in scenes
