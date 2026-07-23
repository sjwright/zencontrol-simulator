"""TCP transport and stream-framing tests."""

import asyncio
from pathlib import Path

import pytest

from zencontrol_simulator.protocol import (
    MAX_TCP_SESSIONS,
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
