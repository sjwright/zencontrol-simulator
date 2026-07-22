"""Event emission matrix for ECG / group / broadcast state-changing commands.

Conventions (matching real controllers / existing simulator behaviour):
- ECG (0–63): events for that ECG only (parent groups are not re-emitted)
- Group (64–79): members + group wire when destinations agree
- Broadcast (255): every ECG + each group wire when that group's members agree
- INHIBIT: mutates state only (no TPI event code)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zencontrol_simulator.events import EventEmitter
from zencontrol_simulator.handlers import CMD, CommandDispatcher
from zencontrol_simulator.protocol import ParseFailure, ResponseType, checksum, parse_request
from zencontrol_simulator.server import Simulator
from zencontrol_simulator.world import FADE_PROGRESS_MIN_MS, load_world

CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"

SCENE = 0x05
COLOUR = 0x08
LEVEL = 0x0B


def _basic(command: int, address: int = 0, d0: int = 0, d1: int = 0, d2: int = 0, seq: int = 1) -> bytes:
    packet = bytearray([0x04, seq, command, address, d0, d1, d2])
    packet.append(checksum(packet))
    return bytes(packet)


def _colour(address: int, level: int, colour: bytes, seq: int = 1) -> bytes:
    packet = bytearray([0x04, seq, CMD["DALI_COLOUR"], address, level])
    packet.extend(colour)
    packet.append(checksum(packet))
    return bytes(packet)


def _disp():
    world = load_world(CONFIG)
    events = EventEmitter(world)
    return CommandDispatcher(world, events), world, events


def _capture(monkeypatch, events):
    emitted: list[tuple[int, int, bytes]] = []
    monkeypatch.setattr(
        events,
        "emit",
        lambda t, c, p=b"", instance=None: emitted.append((t, int(c), bytes(p))) or True,
    )
    return emitted


def _targets(emitted, code: int) -> set[int]:
    return {t for t, c, _ in emitted if c == code}


def _handle(disp, packet: bytes):
    req = parse_request(packet)
    assert not isinstance(req, ParseFailure)
    assert disp.handle(req)[0] == ResponseType.OK


# ---------------------------------------------------------------------------
# ARC / OFF
# ---------------------------------------------------------------------------


def test_ecg_arc_emits_only_that_ecg(monkeypatch):
    disp, world, events = _disp()
    emitted = _capture(monkeypatch, events)
    _handle(disp, _basic(CMD["DALI_ARC_LEVEL"], address=1, d2=40))
    assert _targets(emitted, LEVEL) == {1}
    assert world.lights[1].level == 40
    # Parent groups must not get a companion LEVEL event
    assert 64 not in _targets(emitted, LEVEL)
    assert 65 not in _targets(emitted, LEVEL)


def test_group_arc_emits_members_and_group(monkeypatch):
    disp, world, events = _disp()
    emitted = _capture(monkeypatch, events)
    _handle(disp, _basic(CMD["DALI_ARC_LEVEL"], address=64, d2=40))  # group 0: ECG 0,1
    assert _targets(emitted, LEVEL) == {0, 1, 64}


def test_broadcast_arc_emits_all_ecgs_and_groups(monkeypatch):
    disp, world, events = _disp()
    emitted = _capture(monkeypatch, events)
    _handle(disp, _basic(CMD["DALI_ARC_LEVEL"], address=255, d2=55))
    level_targets = _targets(emitted, LEVEL)
    assert set(world.lights) <= level_targets
    assert {64 + g for g in world.groups} <= level_targets
    assert all(lt.level == 55 for lt in world.lights.values())


def test_broadcast_off_emits_levels(monkeypatch):
    disp, world, events = _disp()
    for lt in world.lights.values():
        lt.set_level(80)
    emitted = _capture(monkeypatch, events)
    _handle(disp, _basic(CMD["DALI_OFF"], address=255))
    assert set(world.lights) <= _targets(emitted, LEVEL)
    assert {64 + g for g in world.groups} <= _targets(emitted, LEVEL)
    assert all(lt.level == 0 for lt in world.lights.values())


# ---------------------------------------------------------------------------
# Relative / recall / last-active
# ---------------------------------------------------------------------------


def test_group_up_emits_group_wire_when_agreed(monkeypatch):
    disp, world, events = _disp()
    world.lights[0].set_level(50)
    world.lights[1].set_level(50)
    world.groups[0].level = 50
    emitted = _capture(monkeypatch, events)
    _handle(disp, _basic(CMD["DALI_UP"], address=64))
    assert _targets(emitted, LEVEL) == {0, 1, 64}
    assert world.lights[0].level == world.lights[1].level == 51


def test_group_go_last_active_omits_group_when_mixed(monkeypatch):
    disp, world, events = _disp()
    world.lights[0].last_active_level = 100
    world.lights[1].last_active_level = 40
    world.lights[0].set_level(0)
    world.lights[1].set_level(0)
    emitted = _capture(monkeypatch, events)
    _handle(disp, _basic(CMD["DALI_GO_TO_LAST_ACTIVE_LEVEL"], address=64))
    assert world.lights[0].level == 100
    assert world.lights[1].level == 40
    assert 0 in _targets(emitted, LEVEL) and 1 in _targets(emitted, LEVEL)
    assert 64 not in _targets(emitted, LEVEL)


def test_broadcast_recall_max_emits_agreed_groups_only(monkeypatch):
    disp, world, events = _disp()
    # Group 2 members (4,5) share max → group wire; group 0 (0,1) differ → no group wire
    world.lights[0].max_level = 100
    world.lights[1].max_level = 50
    world.lights[4].max_level = 200
    world.lights[5].max_level = 200
    emitted = _capture(monkeypatch, events)
    _handle(disp, _basic(CMD["DALI_RECALL_MAX"], address=255))
    levels = _targets(emitted, LEVEL)
    assert 0 in levels and 1 in levels and 4 in levels and 5 in levels
    assert 64 not in levels  # mixed max
    assert 66 in levels  # group 2 agreed


# ---------------------------------------------------------------------------
# Scene
# ---------------------------------------------------------------------------


def test_ecg_scene_emits_scene_level_colour(monkeypatch):
    disp, world, events = _disp()
    emitted = _capture(monkeypatch, events)
    _handle(disp, _basic(CMD["DALI_SCENE"], address=0, d2=1))
    assert 0 in _targets(emitted, SCENE)
    assert 0 in _targets(emitted, LEVEL)
    assert 0 in _targets(emitted, COLOUR)  # scene 1 has TC on ECG 0
    assert 64 not in _targets(emitted, SCENE)


def test_broadcast_scene_emits_members_and_groups(monkeypatch):
    disp, world, events = _disp()
    world.dim_time_ms = 0  # instant so fade_to is cleared
    emitted = _capture(monkeypatch, events)
    _handle(disp, _basic(CMD["DALI_SCENE"], address=255, d2=0))
    scene_targets = _targets(emitted, SCENE)
    assert set(world.lights) <= scene_targets
    assert {64 + g for g in world.groups} <= scene_targets
    level_targets = _targets(emitted, LEVEL)
    # Members with a configured scene 0 level get LEVEL; null-scene ECGs may not change
    assert {0, 1, 2, 4, 5, 6} <= level_targets
    # Group 2 (ECG 4+5 both scene0=200) and group 3 (5+6 both 200) agree
    assert 66 in level_targets and 67 in level_targets
    # Group 0 (180 vs 200) and group 1 (200 vs 128) are mixed
    assert 64 not in level_targets and 65 not in level_targets


# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------


def test_ecg_colour_emits_only_ecg(monkeypatch):
    disp, world, events = _disp()
    emitted = _capture(monkeypatch, events)
    _handle(disp, _colour(0, 0xFF, bytes([0x20, 0x0F, 0xA0])))  # TC
    assert _targets(emitted, COLOUR) == {0}


def test_group_colour_emits_members_and_group(monkeypatch):
    disp, world, events = _disp()
    emitted = _capture(monkeypatch, events)
    _handle(disp, _colour(64, 0xFF, bytes([0x20, 0x0F, 0xA0])))
    assert _targets(emitted, COLOUR) == {0, 1, 64}


# ---------------------------------------------------------------------------
# Custom fade / stop / inhibit
# ---------------------------------------------------------------------------


def test_broadcast_custom_fade_progress_emits_group_wires(monkeypatch):
    import time as time_mod

    world = load_world(CONFIG)
    world.heartbeat_interval = 0
    sim = Simulator(world)
    base = 2_500_000_000.0
    monkeypatch.setattr(time_mod, "time", lambda: base)
    for lt in world.lights.values():
        lt.set_level(0)
    world.apply_level(255, 100, fading_seconds=3.0)

    monkeypatch.setattr(time_mod, "time", lambda: base + 1.0)
    progress = sim.tick_fade_progress()
    member_wires = {w for w, _, _ in progress if w <= 63}
    assert set(world.lights) <= member_wires
    group_wires = {w for w, _, dest in progress if 64 <= w <= 79 and dest == 100}
    assert group_wires == {64 + g for g in world.groups}


def test_broadcast_stop_fade_emits_frozen_levels(monkeypatch):
    import time as time_mod

    disp, world, events = _disp()
    base = 2_600_000_000.0
    monkeypatch.setattr(time_mod, "time", lambda: base)
    for lt in world.lights.values():
        lt.set_level(0)
    world.apply_level(255, 200, fading_seconds=10.0)
    monkeypatch.setattr(time_mod, "time", lambda: base + 2.0)
    emitted = _capture(monkeypatch, events)
    _handle(disp, _basic(CMD["DALI_STOP_FADE"], address=255))
    levels = _targets(emitted, LEVEL)
    assert set(world.lights) <= levels
    # All lights frozen at same interpolated point → groups agree
    assert {64 + g for g in world.groups} <= levels
    frozen = next(iter(world.lights.values())).level
    assert all(lt.level == frozen for lt in world.lights.values())
    assert frozen not in (0, 200)


def test_inhibit_emits_no_events(monkeypatch):
    disp, world, events = _disp()
    emitted = _capture(monkeypatch, events)
    _handle(disp, _basic(CMD["DALI_INHIBIT"], address=1, d1=0, d2=5))
    assert emitted == []
    assert world.lights[1].is_inhibited() is True
    _handle(disp, _basic(CMD["DALI_INHIBIT"], address=64, d1=0, d2=5))
    assert emitted == []
    _handle(disp, _basic(CMD["DALI_INHIBIT"], address=255, d1=0, d2=5))
    assert emitted == []


def test_broadcast_scene_long_fade_progress_includes_groups(monkeypatch):
    import time as time_mod

    world = load_world(CONFIG)
    world.dim_time_ms = FADE_PROGRESS_MIN_MS + 500
    world.heartbeat_interval = 0
    sim = Simulator(world)
    events = EventEmitter(world)
    disp = CommandDispatcher(world, events)
    base = 2_700_000_000.0
    monkeypatch.setattr(time_mod, "time", lambda: base)
    for lt in world.lights.values():
        lt.set_level(0)
    _handle(disp, _basic(CMD["DALI_SCENE"], address=255, d2=0))
    assert any(lt.fade_origin == 255 for lt in world.lights.values())

    monkeypatch.setattr(time_mod, "time", lambda: base + 1.0)
    progress = sim.tick_fade_progress()
    assert any(w <= 63 for w, _, _ in progress)
    # Group 2/3 members share scene 0 destination 200 → group progress wires
    group_progress = {w for w, _, dest in progress if 64 <= w <= 79}
    assert 66 in group_progress and 67 in group_progress
