"""Wire framing for Zencontrol TPI Advanced (UDP)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


MAGIC = 0x04
EVENT_MAGIC = b"\x5a\x43"
DEFAULT_PORT = 5108
MULTICAST_GROUP = "239.255.90.67"
MULTICAST_PORT = 6969


class ResponseType(IntEnum):
    OK = 0xA0
    ANSWER = 0xA1
    NO_ANSWER = 0xA2
    ERROR = 0xA3


class ErrorCode(IntEnum):
    CHECKSUM = 0x01
    UNKNOWN_CMD = 0x04
    INVALID_ARGS = 0xB1
    UNKNOWN_TARGET = 0xB8


class EventCode(IntEnum):
    BUTTON_PRESS = 0x00
    BUTTON_HOLD = 0x01
    ABSOLUTE_INPUT = 0x02
    LEVEL_CHANGE = 0x03
    GROUP_LEVEL_CHANGE = 0x04
    SCENE_CHANGE = 0x05
    IS_OCCUPIED = 0x06
    SYSTEM_VARIABLE_CHANGE = 0x07
    COLOUR_CHANGE = 0x08
    PROFILE_CHANGE = 0x09
    GROUP_OCCUPIED = 0x0A
    LEVEL_CHANGE_V2 = 0x0B


# Commands that use a non-BASIC request payload on the wire.
DYNAMIC_COMMANDS = {0x40}  # SET_TPI_EVENT_UNICAST_ADDRESS
VARIABLE_COMMANDS = {0x0E}  # DALI_COLOUR


def checksum(buf: bytes | bytearray) -> int:
    acc = 0
    for byte in buf:
        acc ^= byte
    return acc & 0xFF


@dataclass(frozen=True)
class Request:
    seq: int
    command: int
    data: bytes
    raw: bytes


@dataclass(frozen=True)
class ParseFailure:
    """Request could not be accepted; optionally reply with an ERROR frame."""

    seq: int
    error: ErrorCode
    reason: str


def parse_request(datagram: bytes) -> Request | ParseFailure | None:
    """Parse a client request.

    Returns:
      - Request on success
      - ParseFailure when we should reply with ERROR (bad checksum / framing)
      - None when the datagram is too corrupt to answer
    """
    if len(datagram) < 4 or datagram[0] != MAGIC:
        return None

    seq = datagram[1]
    if len(datagram) < 5:
        return ParseFailure(seq=seq, error=ErrorCode.INVALID_ARGS, reason="truncated")

    if checksum(datagram[:-1]) != datagram[-1]:
        return ParseFailure(seq=seq, error=ErrorCode.CHECKSUM, reason="checksum")

    command = datagram[2]
    payload = datagram[3:-1]

    if command in DYNAMIC_COMMANDS:
        if not payload:
            return ParseFailure(seq=seq, error=ErrorCode.INVALID_ARGS, reason="dynamic empty")
        length = payload[0]
        data = payload[1 : 1 + length]
        if len(data) != length:
            return ParseFailure(seq=seq, error=ErrorCode.INVALID_ARGS, reason="dynamic length")
    elif command in VARIABLE_COMMANDS:
        if len(payload) < 2:
            return ParseFailure(seq=seq, error=ErrorCode.INVALID_ARGS, reason="colour short")
        data = payload
    elif len(payload) == 4:
        data = payload
    else:
        # Strict basic frame: everything else we support is BASIC (4 data bytes)
        return ParseFailure(seq=seq, error=ErrorCode.INVALID_ARGS, reason="expected basic 4-byte payload")

    return Request(seq=seq, command=command, data=data, raw=datagram)


def build_response(response_type: ResponseType, seq: int, data: bytes = b"") -> bytes:
    if len(data) > 255:
        raise ValueError("Response data exceeds 255 bytes")
    packet = bytearray([response_type & 0xFF, seq & 0xFF, len(data) & 0xFF])
    packet.extend(data)
    packet.append(checksum(packet))
    return bytes(packet)


def build_error(seq: int, error: ErrorCode) -> bytes:
    return build_response(ResponseType.ERROR, seq, bytes([int(error) & 0xFF]))


def build_event(mac: bytes, target: int, event_code: int, payload: bytes = b"") -> bytes:
    if len(mac) != 6:
        raise ValueError("MAC must be 6 bytes")
    if len(payload) > 48:
        raise ValueError("Event payload exceeds 48 bytes")
    packet = bytearray(EVENT_MAGIC)
    packet.extend(mac)
    packet.extend(int(target).to_bytes(2, "big"))
    packet.append(event_code & 0xFF)
    packet.append(len(payload) & 0xFF)
    packet.extend(payload)
    packet.append(checksum(packet))
    return bytes(packet)


def mac_from_string(mac: str) -> bytes:
    cleaned = mac.replace(":", "").replace("-", "").strip()
    if len(cleaned) != 12:
        raise ValueError(f"MAC must be 6 bytes, got {mac!r}")
    return bytes.fromhex(cleaned)


def command_name(command: int, names: dict[str, int]) -> str:
    for name, code in names.items():
        if code == command:
            return name
    return f"0x{command:02X}"
