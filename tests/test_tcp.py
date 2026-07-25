"""TCP transport and stream-framing tests."""

import asyncio
from pathlib import Path

import pytest

from zencontrol_simulator.protocol import (
    MAX_TCP_SESSIONS,
    ErrorCode,
    ResponseType,
    checksum,
    extract_request_frame,
    request_frame_size,
)
from zencontrol_simulator.handlers import CMD
from zencontrol_simulator.server import Simulator
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


def _colour_tc(address: int = 0, kelvin: int = 4000, level: int = 0xFF, seq: int = 1) -> bytes:
    packet = bytearray([0x04, seq, 0x0E, address, level, 0x20, (kelvin >> 8) & 0xFF, kelvin & 0xFF])
    packet.append(checksum(packet))
    return bytes(packet)


def test_request_frame_size_basic():
    frame = _basic(CMD["QUERY_CONTROLLER_LABEL"])
    assert request_frame_size(frame) == 8
    assert request_frame_size(frame[:4]) is None
    assert request_frame_size(b"\x00" + frame) == 0


def test_request_frame_size_dynamic():
    frame = _dynamic(0x40, bytes([0x13, 0xEC, 127, 0, 0, 1]))
    assert request_frame_size(frame) == len(frame)
    assert request_frame_size(frame[:5]) is None


def test_request_frame_size_colour():
    frame = _colour_tc()
    assert len(frame) == 9
    assert request_frame_size(frame) == 9
    assert request_frame_size(frame[:6]) is None


def test_extract_pipelines_two_basics():
    a = _basic(CMD["QUERY_CONTROLLER_LABEL"], seq=1)
    b = _basic(CMD["QUERY_CONTROLLER_LABEL"], seq=2)
    buf = bytearray(a + b)
    assert extract_request_frame(buf) == a
    assert extract_request_frame(buf) == b
    assert extract_request_frame(buf) is None


def test_extract_skips_leading_garbage():
    frame = _basic(CMD["QUERY_CONTROLLER_LABEL"])
    buf = bytearray(b"\x00\xff" + frame)
    assert extract_request_frame(buf) == frame


@pytest.mark.asyncio
async def test_tcp_query_controller_label():
    world = load_world(CONFIG)
    world.bind_host = "127.0.0.1"
    world.bind_port = 0
    world.heartbeat_interval = 0
    sim = Simulator(world)
    await sim.start()
    port = sim.bind_port
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(_basic(CMD["QUERY_CONTROLLER_LABEL"], seq=7))
        await writer.drain()
        header = await asyncio.wait_for(reader.readexactly(3), timeout=1.0)
        assert header[0] == ResponseType.ANSWER
        assert header[1] == 7
        data_len = header[2]
        rest = await asyncio.wait_for(reader.readexactly(data_len + 1), timeout=1.0)
        assert rest[:data_len] == world.label.encode("ascii")
        assert checksum(header + rest[:-1]) == rest[-1]
        writer.close()
        await writer.wait_closed()
    finally:
        await sim.stop()


@pytest.mark.asyncio
async def test_tcp_pipelined_requests():
    world = load_world(CONFIG)
    world.bind_host = "127.0.0.1"
    world.bind_port = 0
    world.heartbeat_interval = 0
    sim = Simulator(world)
    await sim.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", sim.bind_port)
        writer.write(
            _basic(CMD["QUERY_CONTROLLER_LABEL"], seq=1)
            + _basic(CMD["QUERY_IS_DALI_READY"], seq=2)
        )
        await writer.drain()

        async def read_response() -> bytes:
            header = await asyncio.wait_for(reader.readexactly(3), timeout=1.0)
            body = await asyncio.wait_for(reader.readexactly(header[2] + 1), timeout=1.0)
            return header + body

        r1 = await read_response()
        r2 = await read_response()
        assert r1[0] == ResponseType.ANSWER and r1[1] == 1
        assert r2[0] == ResponseType.OK and r2[1] == 2
        writer.close()
        await writer.wait_closed()
    finally:
        await sim.stop()


@pytest.mark.asyncio
async def test_tcp_max_sessions():
    world = load_world(CONFIG)
    world.bind_host = "127.0.0.1"
    world.bind_port = 0
    world.heartbeat_interval = 0
    sim = Simulator(world)
    await sim.start()
    clients: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []
    try:
        for _ in range(MAX_TCP_SESSIONS):
            clients.append(await asyncio.open_connection("127.0.0.1", sim.bind_port))
        await asyncio.sleep(0.05)
        assert sim._tcp_sessions == MAX_TCP_SESSIONS

        # Sixth connection is accepted then immediately closed.
        reader, writer = await asyncio.open_connection("127.0.0.1", sim.bind_port)
        data = await asyncio.wait_for(reader.read(16), timeout=1.0)
        assert data == b""
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        await asyncio.sleep(0.05)
        assert sim._tcp_sessions == MAX_TCP_SESSIONS
    finally:
        for _, writer in clients:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
        await sim.stop()


@pytest.fixture
async def tcp_sim():
    """Simulator on an ephemeral port with background event loops disabled."""
    world = load_world(CONFIG)
    world.bind_host = "127.0.0.1"
    world.bind_port = 0
    world.heartbeat_interval = 0
    sim = Simulator(world)
    await sim.start()
    try:
        yield sim
    finally:
        await sim.stop()


async def _read_response(reader: asyncio.StreamReader) -> bytes:
    header = await asyncio.wait_for(reader.readexactly(3), timeout=1.0)
    body = await asyncio.wait_for(reader.readexactly(header[2] + 1), timeout=1.0)
    return header + body


@pytest.mark.asyncio
async def test_tcp_reassembles_frame_split_byte_by_byte(tcp_sim):
    """A TCP peer may flush a frame one byte at a time; the server must buffer."""
    reader, writer = await asyncio.open_connection("127.0.0.1", tcp_sim.bind_port)
    try:
        for byte in _basic(CMD["QUERY_CONTROLLER_LABEL"], seq=11):
            writer.write(bytes([byte]))
            await writer.drain()
            await asyncio.sleep(0)
        resp = await _read_response(reader)
        assert resp[0] == ResponseType.ANSWER
        assert resp[1] == 11
        assert resp[3 : 3 + resp[2]] == tcp_sim.world.label.encode("ascii")
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_tcp_reassembles_dynamic_frame_split_mid_payload(tcp_sim):
    """Split inside the variable-length body, where the length byte is already
    buffered but the payload is not."""
    frame = _dynamic(
        CMD["SET_TPI_EVENT_UNICAST_ADDRESS"],
        bytes([0x1B, 0x3A, 127, 0, 0, 1]),  # port 6970, then IPv4
        seq=12,
    )
    reader, writer = await asyncio.open_connection("127.0.0.1", tcp_sim.bind_port)
    try:
        writer.write(frame[:5])
        await writer.drain()
        await asyncio.sleep(0.05)
        writer.write(frame[5:])
        await writer.drain()
        resp = await _read_response(reader)
        assert resp[0] == ResponseType.OK
        assert resp[1] == 12
        assert tcp_sim.world.unicast_ip == "127.0.0.1"
        assert tcp_sim.world.unicast_port == 6970
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_tcp_coalesced_frames_split_across_a_boundary(tcp_sim):
    """Two frames arriving as segments that straddle the frame boundary."""
    stream = _basic(CMD["QUERY_CONTROLLER_LABEL"], seq=1) + _basic(
        CMD["QUERY_IS_DALI_READY"], seq=2
    )
    reader, writer = await asyncio.open_connection("127.0.0.1", tcp_sim.bind_port)
    try:
        writer.write(stream[:5])
        await writer.drain()
        await asyncio.sleep(0.05)
        writer.write(stream[5:])
        await writer.drain()
        r1 = await _read_response(reader)
        r2 = await _read_response(reader)
        assert (r1[0], r1[1]) == (ResponseType.ANSWER, 1)
        assert (r2[0], r2[1]) == (ResponseType.OK, 2)
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_tcp_bad_checksum_errors_and_session_survives(tcp_sim):
    """A corrupt frame answers ERROR_CHECKSUM without dropping the connection."""
    corrupt = bytearray(_basic(CMD["QUERY_CONTROLLER_LABEL"], seq=21))
    corrupt[-1] ^= 0xFF
    reader, writer = await asyncio.open_connection("127.0.0.1", tcp_sim.bind_port)
    try:
        writer.write(bytes(corrupt))
        await writer.drain()
        bad = await _read_response(reader)
        assert bad[0] == ResponseType.ERROR
        assert bad[1] == 21
        assert bad[3] == ErrorCode.CHECKSUM

        writer.write(_basic(CMD["QUERY_CONTROLLER_LABEL"], seq=22))
        await writer.drain()
        good = await _read_response(reader)
        assert good[0] == ResponseType.ANSWER
        assert good[1] == 22
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_tcp_resyncs_after_leading_garbage(tcp_sim):
    """Bytes that cannot start a frame are discarded until a valid one appears."""
    reader, writer = await asyncio.open_connection("127.0.0.1", tcp_sim.bind_port)
    try:
        writer.write(b"\x00\xff\x99" + _basic(CMD["QUERY_CONTROLLER_LABEL"], seq=31))
        await writer.drain()
        resp = await _read_response(reader)
        assert resp[0] == ResponseType.ANSWER
        assert resp[1] == 31
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_tcp_unknown_command_errors(tcp_sim):
    reader, writer = await asyncio.open_connection("127.0.0.1", tcp_sim.bind_port)
    try:
        writer.write(_basic(0x7E, seq=41))
        await writer.drain()
        resp = await _read_response(reader)
        assert resp[0] == ResponseType.ERROR
        assert resp[3] == ErrorCode.UNKNOWN_CMD
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_udp_and_tcp_share_port():
    world = load_world(CONFIG)
    world.bind_host = "127.0.0.1"
    world.bind_port = 0
    world.heartbeat_interval = 0
    sim = Simulator(world)
    await sim.start()
    port = sim.bind_port
    try:
        # UDP still works
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(1.0)
        loop = asyncio.get_running_loop()
        try:
            sock.sendto(_basic(CMD["QUERY_CONTROLLER_LABEL"], seq=3), ("127.0.0.1", port))
            await asyncio.sleep(0.05)
            data, _ = await loop.run_in_executor(None, sock.recvfrom, 256)
            assert data[0] == ResponseType.ANSWER
            assert data[1] == 3
        finally:
            sock.close()

        # TCP on same port
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(_basic(CMD["QUERY_CONTROLLER_LABEL"], seq=4))
        await writer.drain()
        header = await asyncio.wait_for(reader.readexactly(3), timeout=1.0)
        assert header[0] == ResponseType.ANSWER and header[1] == 4
        await reader.readexactly(header[2] + 1)
        writer.close()
        await writer.wait_closed()
    finally:
        await sim.stop()
