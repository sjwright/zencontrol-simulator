"""Assertions taken directly from the TPI Advanced PDF.

Every test here pins a clause that the simulator previously contradicted, so the
docstrings quote the wording being enforced. When a clause is ambiguous the
divergence is recorded in DOCUMENTATION_ISSUES.md instead of being asserted here.
"""

from pathlib import Path

import pytest

from zencontrol_simulator.events import EventEmitter
from zencontrol_simulator.handlers import (
    CMD,
    LEGACY_ACK_COMMANDS,
    MAX_SCENE,
    CommandDispatcher,
)
from zencontrol_simulator.protocol import (
    ErrorCode,
    EventCode,
    ParseFailure,
    ResponseType,
    checksum,
    parse_request,
)
from zencontrol_simulator.world import load_world

CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"

BROADCAST = 0xFF
UNKNOWN_ECG = 50
UNKNOWN_GROUP_WIRE = 64 + 15


def _basic(command: int, address: int = 0, d0: int = 0, d1: int = 0, d2: int = 0, seq: int = 1) -> bytes:
    packet = bytearray([0x04, seq, command, address, d0, d1, d2])
    packet.append(checksum(packet))
    return bytes(packet)


def _colour(address: int, level: int, colour: bytes, seq: int = 1) -> bytes:
    packet = bytearray([0x04, seq, CMD["DALI_COLOUR"], address, level])
    packet.extend(colour)
    packet.append(checksum(packet))
    return bytes(packet)


def _disp(config: Path = CONFIG):
    world = load_world(config)
    events = EventEmitter(world)
    return CommandDispatcher(world, events), world, events


def _send(disp, packet: bytes) -> bytes:
    req = parse_request(packet)
    assert not isinstance(req, ParseFailure)
    return disp.handle(req)


def _capture(monkeypatch, events):
    emitted: list[tuple[int, int, bytes]] = []
    monkeypatch.setattr(
        events,
        "emit",
        lambda t, c, p=b"", instance=None: emitted.append((t, int(c), bytes(p))) or True,
    )
    return emitted


# ---------------------------------------------------------------------------
# REPLY_NO_ANSWER as the acknowledgement for first-generation commands
# ---------------------------------------------------------------------------

LEGACY_COMMANDS = {
    "DALI_SCENE": dict(address=0, d2=1),
    "DALI_ARC_LEVEL": dict(address=0, d2=100),
    "DALI_ON_STEP_UP": dict(address=0),
    "DALI_STEP_DOWN_OFF": dict(address=0),
    "DALI_UP": dict(address=0),
    "DALI_DOWN": dict(address=0),
    "DALI_RECALL_MAX": dict(address=0),
    "DALI_RECALL_MIN": dict(address=0),
    "DALI_OFF": dict(address=0),
    "DALI_GO_TO_LAST_ACTIVE_LEVEL": dict(address=0),
}


def test_legacy_ack_set_matches_the_documented_ten():
    """The PDF flags exactly these ten commands as answering NO_ANSWER."""
    assert LEGACY_ACK_COMMANDS == {CMD[name] for name in LEGACY_COMMANDS}


@pytest.mark.parametrize("name", sorted(LEGACY_COMMANDS))
def test_legacy_commands_acknowledge_with_no_answer(name):
    """PDF REPLY_NO_ANSWER: "this is expected behaviour for a command. A better
    response would be OK (0xA0) but we must maintain backwards compatibility."."""
    disp, _, _ = _disp()
    assert _send(disp, _basic(CMD[name], **LEGACY_COMMANDS[name]))[0] == ResponseType.NO_ANSWER


@pytest.mark.parametrize(
    "packet",
    [
        _basic(CMD["DALI_INHIBIT"], address=0, d2=10),
        _basic(CMD["DALI_CUSTOM_FADE"], address=0, d0=100, d2=5),
        _basic(CMD["DALI_STOP_FADE"], address=0),
        _colour(0, 0xFF, bytes([0x20, 0x0F, 0xA0])),
    ],
    ids=["inhibit", "custom_fade", "stop_fade", "colour"],
)
def test_later_level_commands_still_acknowledge_with_ok(packet):
    """These four carry a REPLY_OK response table in the PDF."""
    disp, _, _ = _disp()
    assert _send(disp, packet)[0] == ResponseType.OK


def test_dapc_sequence_acknowledges_with_no_answer():
    """DALI_ENABLE_DAPC_SEQ carries the same NO_ANSWER wording as the legacy ten
    even though it does not change a level itself."""
    disp, _, _ = _disp()
    assert _send(disp, _basic(CMD["DALI_ENABLE_DAPC_SEQ"], address=0))[0] == (
        ResponseType.NO_ANSWER
    )


def test_legacy_ack_does_not_mask_addressing_errors():
    """An unroutable target must still be REPLY_ERROR, not a bare NO_ANSWER."""
    disp, _, _ = _disp()
    resp = _send(disp, _basic(CMD["DALI_ARC_LEVEL"], address=UNKNOWN_GROUP_WIRE, d2=10))
    assert resp[0] == ResponseType.ERROR
    assert resp[3] == ErrorCode.UNKNOWN_TARGET


# ---------------------------------------------------------------------------
# Queries that answer a default rather than declining
# ---------------------------------------------------------------------------

EMPTY_GROUP_YAML = """
controller:
  mac: "02:00:00:00:00:01"
  label: "X"
  version: [2, 2, 11]
groups:
  - number: 3
    label: "Spare"
    level: 200
lights: []
devices: []
profiles:
  items: []
system_variables: []
"""


@pytest.mark.parametrize(
    "address",
    [UNKNOWN_ECG, UNKNOWN_GROUP_WIRE, BROADCAST],
    ids=["unknown_ecg", "unknown_group", "broadcast"],
)
def test_query_level_answers_zero_for_unknown_target(address):
    """PDF DALI_QUERY_LEVEL: "If the address does not exist in the database (or
    the group has no devices) the response will be 0. This is to bias any
    resulting decision to send commands to this unknown target as turning the
    light ON."."""
    disp, _, _ = _disp()
    resp = _send(disp, _basic(CMD["DALI_QUERY_LEVEL"], address=address))
    assert resp[0] == ResponseType.ANSWER
    assert resp[2] == 1
    assert resp[3] == 0


def test_query_level_answers_zero_for_group_with_no_devices(tmp_path):
    """Same clause: a configured group with no members answers 0, not its own
    stored level."""
    cfg = tmp_path / "empty_group.yaml"
    cfg.write_text(EMPTY_GROUP_YAML)
    disp, world, _ = _disp(cfg)
    assert world.groups[3].level == 200
    resp = _send(disp, _basic(CMD["DALI_QUERY_LEVEL"], address=64 + 3))
    assert resp[0] == ResponseType.ANSWER
    assert resp[3] == 0


@pytest.mark.parametrize(
    "address",
    [UNKNOWN_ECG, 64 + 0, BROADCAST],
    ids=["unknown_ecg", "group", "broadcast"],
)
def test_query_cg_type_answers_all_zero_for_non_gear(address):
    """PDF DALI_QUERY_CG_TYPE: "does not work for groups or broadcast target. If
    the device does not exist, will return all zero."."""
    disp, _, _ = _disp()
    resp = _send(disp, _basic(CMD["DALI_QUERY_CG_TYPE"], address=address))
    assert resp[0] == ResponseType.ANSWER
    assert resp[2] == 4
    assert resp[3:7] == bytes(4)


def test_query_cg_type_still_reports_real_gear():
    """The default must not swallow a genuine answer."""
    disp, world, _ = _disp()
    resp = _send(disp, _basic(CMD["DALI_QUERY_CG_TYPE"], address=0))
    assert resp[0] == ResponseType.ANSWER
    assert resp[3:7] != bytes(4)
    assert world.lights[0].cg_types


# ---------------------------------------------------------------------------
# QUERY_DALI_ADDRESSES_WITH_INSTANCES pages by control device number
# ---------------------------------------------------------------------------


def test_addresses_with_instances_start_is_a_control_device_number():
    """PDF: "run the command with start address 0 and then a further command with
    start address 60 to check for instances on the final 4 control devices."
    The reply carries wire addresses (64 + control device number)."""
    disp, world, _ = _disp()
    resp = _send(disp, _basic(CMD["QUERY_DALI_ADDRESSES_WITH_INSTANCES"], d2=0))
    assert resp[0] == ResponseType.ANSWER
    assert list(resp[3 : 3 + resp[2]]) == [64 + a for a in sorted(world.devices)]


def test_addresses_with_instances_paging_skips_lower_devices():
    disp, world, _ = _disp()
    lowest = min(world.devices)
    resp = _send(
        disp, _basic(CMD["QUERY_DALI_ADDRESSES_WITH_INSTANCES"], d2=lowest + 1)
    )
    expected = [64 + a for a in sorted(world.devices) if a >= lowest + 1]
    assert list(resp[3 : 3 + resp[2]]) == expected
    assert 64 + lowest not in resp[3:]


def test_addresses_with_instances_second_page_is_empty():
    """Start 60 exceeds every configured control device in the demo world."""
    disp, _, _ = _disp()
    assert _send(disp, _basic(CMD["QUERY_DALI_ADDRESSES_WITH_INSTANCES"], d2=60))[0] == (
        ResponseType.NO_ANSWER
    )


# ---------------------------------------------------------------------------
# CHANGE_PROFILE_NUMBER refusal
# ---------------------------------------------------------------------------


def test_unknown_profile_is_refused_not_invalid():
    """PDF CHANGE_PROFILE_NUMBER: "A failure to schedule will reply with a
    response type of REPLY_ERROR and ERROR_CMD_REFUSED as data."."""
    disp, _, _ = _disp()
    resp = _send(disp, _basic(CMD["CHANGE_PROFILE_NUMBER"], d1=0x00, d2=0x7E))
    assert resp[0] == ResponseType.ERROR
    assert resp[3] == ErrorCode.CMD_REFUSED == 0xB2


def test_known_profile_is_accepted():
    disp, world, _ = _disp()
    target = next(p for p in world.profiles if p != world.current_profile)
    resp = _send(
        disp,
        _basic(CMD["CHANGE_PROFILE_NUMBER"], d1=(target >> 8) & 0xFF, d2=target & 0xFF),
    )
    assert resp[0] == ResponseType.OK
    assert world.current_profile == target


# ---------------------------------------------------------------------------
# COLOUR_CHANGED_EVENT width follows the fixture's channel count
# ---------------------------------------------------------------------------


def _rgb_light(world) -> int:
    return next(
        a
        for a, lt in world.lights.items()
        if lt.colour_features.rgbwaf_channels == 3
    )


def test_colour_event_for_three_channel_fixture_is_four_bytes(monkeypatch):
    """PDF COLOUR_CHANGED_EVENT: "If a fixture is just RGB or RGBW (and not
    RGBWAF) then the data length will be equal to the number of channels + 1."."""
    disp, world, events = _disp()
    emitted = _capture(monkeypatch, events)
    addr = _rgb_light(world)
    _send(disp, _colour(addr, 0xFF, bytes([0x80, 10, 20, 30, 0, 0, 0])))
    payloads = [p for t, c, p in emitted if c == EventCode.COLOUR_CHANGE and t == addr]
    assert payloads == [bytes([0x80, 10, 20, 30])]


def test_colour_query_stays_full_width(monkeypatch):
    """Only the event narrows: QUERY_DALI_COLOUR is documented with 7 data bytes."""
    disp, world, _ = _disp()
    addr = _rgb_light(world)
    _send(disp, _colour(addr, 0xFF, bytes([0x80, 10, 20, 30, 0, 0, 0])))
    resp = _send(disp, _basic(CMD["QUERY_DALI_COLOUR"], address=addr))
    assert resp[0] == ResponseType.ANSWER
    assert resp[2] == 7
    assert resp[3:10] == bytes([0x80, 10, 20, 30, 0, 0, 0])


def test_tunable_white_event_is_unaffected(monkeypatch):
    """Tc carries a fixed 3-byte payload regardless of channel count."""
    disp, world, events = _disp()
    emitted = _capture(monkeypatch, events)
    addr = next(
        a for a, lt in world.lights.items() if lt.colour_features.supports_tunable
    )
    _send(disp, _colour(addr, 0xFF, bytes([0x20, 0x0F, 0xA0])))
    payloads = [p for t, c, p in emitted if c == EventCode.COLOUR_CHANGE and t == addr]
    assert payloads == [bytes([0x20, 0x0F, 0xA0])]


# ---------------------------------------------------------------------------
# GROUP_OCCUPANCY_EVENT state-change semantics
# ---------------------------------------------------------------------------


def _sensor_with_group(world) -> tuple[int, int, int]:
    for ecd in sorted(world.devices):
        for inst in world.devices[ecd].instances:
            if inst.type != 0x03:
                continue
            targets = [g for g in inst.groups if g != 0xFF]
            if targets:
                return ecd, inst.number, targets[0]
    raise AssertionError("demo world has no group-targeting occupancy sensor")


def test_group_occupancy_is_emitted_once_per_state_change(monkeypatch):
    """PDF GROUP_OCCUPANCY_EVENT: "Does not send occupied on every trigger from
    sensor, only on state changes. If a group has already been set to occupied,
    you will not hear a message until 30 seconds without triggers occurs."."""
    disp, world, events = _disp()
    emitted = _capture(monkeypatch, events)
    ecd, instance, group = _sensor_with_group(world)

    events.occupancy(ecd, instance)
    events.occupancy(ecd, instance)
    events.occupancy(ecd, instance)

    group_events = [
        (t, p) for t, c, p in emitted if c == EventCode.GROUP_OCCUPIED
    ]
    assert group_events == [(64 + group, bytes([0xFF, 0x01]))]
    assert world.groups[group].is_occupied()
    assert sum(1 for _, c, _ in emitted if c == EventCode.IS_OCCUPIED) == 3


def test_group_occupancy_clears_after_seconds_to_unoccupied(monkeypatch):
    """The hold defaults to 30s; expiry emits the matching "not occupied"."""
    disp, world, events = _disp()
    world.seconds_to_unoccupied = 0
    emitted = _capture(monkeypatch, events)
    ecd, instance, group = _sensor_with_group(world)

    events.occupancy(ecd, instance)
    assert events.expire_group_occupancy() == [group]
    assert not world.groups[group].is_occupied()

    group_events = [(t, p) for t, c, p in emitted if c == EventCode.GROUP_OCCUPIED]
    assert group_events == [
        (64 + group, bytes([0xFF, 0x01])),
        (64 + group, bytes([0xFF, 0x00])),
    ]


def test_group_occupancy_re_arms_after_expiry(monkeypatch):
    disp, world, events = _disp()
    world.seconds_to_unoccupied = 0
    emitted = _capture(monkeypatch, events)
    ecd, instance, group = _sensor_with_group(world)

    events.occupancy(ecd, instance)
    events.expire_group_occupancy()
    events.occupancy(ecd, instance)

    occupied = [p[1] for t, c, p in emitted if c == EventCode.GROUP_OCCUPIED]
    assert occupied == [1, 0, 1]


def test_group_occupancy_hold_default_is_thirty_seconds():
    _, world, _ = _disp()
    assert world.seconds_to_unoccupied == 30


def test_sensor_without_group_target_emits_no_group_event(monkeypatch):
    disp, world, events = _disp()
    emitted = _capture(monkeypatch, events)
    ecd, instance = next(
        (a, i.number)
        for a in sorted(world.devices)
        for i in world.devices[a].instances
        if i.type == 0x03 and all(g == 0xFF for g in i.groups)
    )
    events.occupancy(ecd, instance)
    assert not [t for t, c, _ in emitted if c == EventCode.GROUP_OCCUPIED]


# ---------------------------------------------------------------------------
# Scene ceiling. The PDF describes a 16-slot DALI scene table under a cloud
# layer that names 12, without stating how they relate or where DALI_SCENE
# stops; we cap at the cloud layer (DOCUMENTATION_ISSUES.md §1.1).
# ---------------------------------------------------------------------------


def test_scene_ceiling_is_twelve_scenes():
    disp, _, _ = _disp()
    assert MAX_SCENE == 12
    assert _send(disp, _basic(CMD["DALI_SCENE"], address=0, d2=11))[0] == (
        ResponseType.NO_ANSWER
    )
    resp = _send(disp, _basic(CMD["DALI_SCENE"], address=0, d2=12))
    assert resp[0] == ResponseType.ERROR
    assert resp[3] == ErrorCode.INVALID_ARGS
