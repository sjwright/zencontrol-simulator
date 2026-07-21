"""Unit tests for framing, handlers, and event injection."""

from __future__ import annotations

from pathlib import Path

import pytest

from zencontrol_simulator.events import EventEmitter
from zencontrol_simulator.handlers import CMD, CommandDispatcher
from zencontrol_simulator.protocol import (
    ErrorCode,
    ParseFailure,
    ResponseType,
    build_error,
    build_event,
    build_response,
    checksum,
    parse_request,
)
from zencontrol_simulator.world import load_world

CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"


def _basic(command: int, address: int = 0, d0: int = 0, d1: int = 0, d2: int = 0, seq: int = 1) -> bytes:
    packet = bytearray([0x04, seq, command, address, d0, d1, d2])
    packet.append(checksum(packet))
    return bytes(packet)


def test_checksum_roundtrip():
    body = bytes([0x04, 0x00, 0x01, 0x0A, 0x00, 0x00, 0x00])
    cs = checksum(body)
    assert cs == 0x0F
    assert checksum(body + bytes([cs])) == 0


def test_parse_basic_and_response():
    req = parse_request(_basic(0x24))
    assert not isinstance(req, ParseFailure)
    assert req is not None
    assert req.command == 0x24
    resp = build_response(ResponseType.ANSWER, req.seq, b"Sim")
    assert resp[0] == ResponseType.ANSWER
    assert resp[1] == req.seq
    assert resp[2] == 3
    assert resp[3:6] == b"Sim"
    assert checksum(resp[:-1]) == resp[-1]


def test_parse_bad_checksum_returns_failure():
    packet = bytearray(_basic(0x24))
    packet[-1] ^= 0xFF
    result = parse_request(bytes(packet))
    assert isinstance(result, ParseFailure)
    assert result.error == ErrorCode.CHECKSUM
    err = build_error(result.seq, result.error)
    assert err[0] == ResponseType.ERROR
    assert err[3] == ErrorCode.CHECKSUM


def test_parse_rejects_wrong_basic_length():
    packet = bytearray([0x04, 0x01, 0x24, 0x00, 0x00])  # too short before checksum
    packet.append(checksum(packet))
    result = parse_request(bytes(packet))
    assert isinstance(result, ParseFailure)
    assert result.error == ErrorCode.INVALID_ARGS


def test_event_frame():
    mac = bytes.fromhex("020000000001")
    packet = build_event(mac, 5, 0x0B, bytes([0xFE, 0x00]))
    assert packet[:2] == b"ZC"
    assert packet[2:8] == mac
    assert int.from_bytes(packet[8:10], "big") == 5
    assert packet[10] == 0x0B
    assert packet[11] == 2
    assert checksum(packet[:-1]) == packet[-1]


@pytest.fixture
def dispatcher():
    world = load_world(CONFIG)
    events = EventEmitter(world)
    return CommandDispatcher(world, events), world, events


def test_load_config_features(dispatcher):
    _, world, _ = dispatcher
    assert 8 in world.lights[0].cg_types
    assert world.lights[0].colour_features.supports_tunable
    assert world.lights[2].colour_features.rgbwaf_channels == 3


def test_query_controller_label(dispatcher):
    disp, world, _ = dispatcher
    req = parse_request(_basic(CMD["QUERY_CONTROLLER_LABEL"]))
    assert req is not None and not isinstance(req, ParseFailure)
    resp = disp.handle(req)
    assert resp[0] == ResponseType.ANSWER
    assert resp[3:-1] == world.label.encode("ascii")


def test_query_gear_bitmap(dispatcher):
    disp, world, _ = dispatcher
    req = parse_request(_basic(CMD["QUERY_CONTROL_GEAR_DALI_ADDRESSES"]))
    assert req is not None and not isinstance(req, ParseFailure)
    resp = disp.handle(req)
    assert resp[0] == ResponseType.ANSWER
    bitmap = resp[3:-1]
    assert len(bitmap) == 8
    for addr in world.lights:
        assert bitmap[addr // 8] & (1 << (addr % 8))


def test_arc_level_emits_event(dispatcher, monkeypatch):
    disp, world, events = dispatcher
    emitted = []
    before = world.lights[0].level

    def capture(target, code, payload=b"", instance=None):
        emitted.append((target, int(code), payload))
        return True

    monkeypatch.setattr(events, "emit", capture)
    req = parse_request(_basic(CMD["DALI_ARC_LEVEL"], address=0, d2=100))
    assert req is not None and not isinstance(req, ParseFailure)
    resp = disp.handle(req)
    assert resp[0] == ResponseType.OK
    assert world.lights[0].level == 100
    assert emitted[0] == (0, 0x0B, bytes([before, 100]))


def test_arc_unknown_target(dispatcher):
    disp, _, _ = dispatcher
    req = parse_request(_basic(CMD["DALI_ARC_LEVEL"], address=63, d2=10))
    assert req is not None and not isinstance(req, ParseFailure)
    resp = disp.handle(req)
    assert resp[0] == ResponseType.ERROR
    assert resp[3] == ErrorCode.UNKNOWN_TARGET


def test_unsupported_command(dispatcher):
    disp, _, _ = dispatcher
    req = parse_request(_basic(0x17))  # DMX
    assert req is not None and not isinstance(req, ParseFailure)
    resp = disp.handle(req)
    assert resp[0] == ResponseType.ERROR
    assert resp[3] == ErrorCode.UNKNOWN_CMD


def test_startup_complete_ok(dispatcher):
    disp, _, _ = dispatcher
    req = parse_request(_basic(CMD["QUERY_CONTROLLER_STARTUP_COMPLETE"]))
    assert req is not None and not isinstance(req, ParseFailure)
    resp = disp.handle(req)
    assert resp[0] == ResponseType.OK


def test_enable_events_echo(dispatcher):
    disp, world, _ = dispatcher
    req = parse_request(_basic(CMD["ENABLE_TPI_EVENT_EMIT"], address=0x41))
    assert req is not None and not isinstance(req, ParseFailure)
    resp = disp.handle(req)
    assert resp[0] == ResponseType.ANSWER
    assert resp[3] == 0x41
    assert world.event_mode == 0x41


def test_scene_levels_twelve_bytes(dispatcher):
    disp, _, _ = dispatcher
    req = parse_request(_basic(CMD["QUERY_SCENE_LEVELS_BY_ADDRESS"], address=0))
    assert req is not None and not isinstance(req, ParseFailure)
    resp = disp.handle(req)
    assert resp[0] == ResponseType.ANSWER
    assert resp[2] == 12


def test_instances_and_buttons(dispatcher):
    disp, _, _ = dispatcher
    req = parse_request(_basic(CMD["QUERY_DALI_ADDRESSES_WITH_INSTANCES"], d2=0))
    assert req is not None and not isinstance(req, ParseFailure)
    resp = disp.handle(req)
    assert resp[0] == ResponseType.ANSWER
    assert 64 in resp[3:-1]
    assert 65 in resp[3:-1]

    req2 = parse_request(_basic(CMD["QUERY_INSTANCES_BY_ADDRESS"], address=64))
    assert req2 is not None and not isinstance(req2, ParseFailure)
    resp2 = disp.handle(req2)
    assert resp2[0] == ResponseType.ANSWER
    assert resp2[2] == 12  # 3 instances × 4 bytes


def test_button_inject(dispatcher, monkeypatch):
    _, world, events = dispatcher
    sent = []

    def capture(target, code, payload=b"", instance=None):
        sent.append((target, int(code), payload, instance))
        return True

    monkeypatch.setattr(events, "emit", capture)
    assert events.button_press(0, 0)
    assert sent[0][0] == 64
    assert sent[0][1] == 0x00
    assert sent[0][2] == b"\x00"

    with pytest.raises(ValueError):
        events.button_press(0, 2)  # occupancy, not button


def test_colour_scene_blob_length(dispatcher):
    disp, _, _ = dispatcher
    req = parse_request(_basic(CMD["QUERY_COLOUR_SCENE_0_7_DATA_FOR_ADDR"], address=0))
    assert req is not None and not isinstance(req, ParseFailure)
    resp = disp.handle(req)
    assert resp[0] == ResponseType.ANSWER
    assert resp[2] == 56  # 8 × 7
