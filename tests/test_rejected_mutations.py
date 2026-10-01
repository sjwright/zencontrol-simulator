"""Rejected wire commands must not mutate controller state or emit events."""

import asyncio
from dataclasses import asdict
from pathlib import Path
import socket
from unittest.mock import MagicMock

import pytest

from zencontrol_simulator.handlers import CMD
from zencontrol_simulator.protocol import ErrorCode, ResponseType, checksum
from zencontrol_simulator.server import Simulator
from zencontrol_simulator.world import load_world


def _packet(command, data, seq=17):
    body = bytes([4, seq, command, *data])
    return body + bytes([checksum(body)])


def _invalid_command(case):
    if case == "scene":
        return _packet(CMD["DALI_SCENE"], [0, 0, 0, 12]), ErrorCode.INVALID_ARGS
    if case == "colour":
        return _packet(CMD["DALI_COLOUR"], [0, 80, 1, 0x12, 0x34]), ErrorCode.INVALID_ARGS
    if case == "unknown-target":
        return _packet(CMD["DALI_ARC_LEVEL"], [63, 0, 0, 100]), ErrorCode.UNKNOWN_TARGET
    if case == "unknown-sysvar":
        return _packet(CMD["SET_SYSTEM_VARIABLE"], [99, 0, 0, 100]), ErrorCode.INVALID_ARGS
    if case == "unicast-length":
        return _packet(CMD["SET_TPI_EVENT_UNICAST_ADDRESS"], [5, 0x1B, 0x39, 127, 0, 0]), ErrorCode.INVALID_ARGS
    packet = bytearray(_packet(CMD["DALI_ARC_LEVEL"], [0, 0, 0, 100]))
    packet[-1] ^= 0xFF
    return bytes(packet), ErrorCode.CHECKSUM


@pytest.fixture(params=["udp", "tcp"])
async def connection(request):
    world = load_world(Path(__file__).resolve().parents[1] / "config.yaml")
    world.bind_host, world.bind_port, world.heartbeat_interval = "127.0.0.1", 0, 0
    for variable in world.system_variables.values():
        variable.simulate = None
    sim = Simulator(world)
    await sim.start()
    sim.events.emit = MagicMock(return_value=True)
    if request.param == "tcp":
        reader, writer = await asyncio.open_connection("127.0.0.1", sim.bind_port)

        async def exchange(packet):
            writer.write(packet)
            await writer.drain()
            header = await asyncio.wait_for(reader.readexactly(3), 1)
            return header + await asyncio.wait_for(reader.readexactly(header[2] + 1), 1)
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        sock.connect(("127.0.0.1", sim.bind_port))
        loop = asyncio.get_running_loop()

        async def exchange(packet):
            await loop.sock_sendall(sock, packet)
            return await asyncio.wait_for(loop.sock_recv(sock, 1024), 1)
    try:
        yield world, sim.events.emit, exchange
    finally:
        if request.param == "tcp":
            writer.close()
            await writer.wait_closed()
        else:
            sock.close()
        await sim.stop()


@pytest.mark.parametrize("case", ["scene", "colour", "unknown-target", "unknown-sysvar", "unicast-length", "checksum"])
async def test_rejected_mutation_is_atomic_and_connection_survives(connection, case):
    world, emit, exchange = connection
    before = asdict(world)
    packet, error = _invalid_command(case)
    response = await exchange(packet)
    assert response[:4] == bytes([ResponseType.ERROR, 17, 1, error])
    assert checksum(response) == 0
    assert asdict(world) == before
    emit.assert_not_called()

    # Reuse the same connection to prove rejection did not poison the parser.
    response = await exchange(_packet(CMD["DALI_ARC_LEVEL"], [0, 0, 0, 101], seq=18))
    assert response[:3] == bytes([ResponseType.NO_ANSWER, 18, 0])
    assert world.lights[0].level == 101
    assert world.lights[1].level == before["lights"][1]["level"]
    emit.assert_called()
