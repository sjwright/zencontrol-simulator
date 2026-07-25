"""Where TPI events actually land: unicast target, multicast group, or nowhere.

Other suites assert that events are *built* correctly; these assert the datagrams
leave the simulator addressed to the right places for each event mode bit.
"""

import asyncio
import socket
from pathlib import Path

import pytest

from zencontrol_simulator.protocol import (
    MULTICAST_GROUP,
    MULTICAST_PORT,
    EventCode,
    checksum,
)
from zencontrol_simulator.server import Simulator
from zencontrol_simulator.world import load_world

CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"

ENABLED = 0x01
UNICAST = 0x40
MULTICAST_OFF = 0x80


@pytest.fixture
async def sim():
    world = load_world(CONFIG)
    world.bind_host = "127.0.0.1"
    world.bind_port = 0
    world.heartbeat_interval = 0
    simulator = Simulator(world)
    await simulator.start()
    try:
        yield simulator
    finally:
        await simulator.stop()


@pytest.fixture
def listener():
    """Bound UDP socket standing in for a unicast TPI event client."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)
    try:
        yield sock
    finally:
        sock.close()


class _RecordingSocket:
    """Passes datagrams through to the real socket while logging destinations."""

    def __init__(self, inner: socket.socket) -> None:
        self._inner = inner
        self.destinations: list[tuple[str, int]] = []

    def sendto(self, packet: bytes, address: tuple[str, int]):
        self.destinations.append(address)
        return self._inner.sendto(packet, address)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def _spy(monkeypatch, sim) -> list[tuple[str, int]]:
    """Record every destination the emitter sends to, without blocking sends."""
    proxy = _RecordingSocket(sim.events._sock)
    monkeypatch.setattr(sim.events, "_sock", proxy)
    return proxy.destinations


def _target_unicast(sim, sock: socket.socket) -> None:
    sim.world.unicast_ip, sim.world.unicast_port = sock.getsockname()


async def _recv(sock: socket.socket, timeout: float = 1.0) -> bytes:
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(loop.sock_recv(sock, 512), timeout=timeout)


@pytest.mark.asyncio
async def test_unicast_only_reaches_configured_client(sim, listener, monkeypatch):
    """With the multicast bit set (disabled), the only datagram goes to the
    configured unicast address."""
    sim.world.event_mode = ENABLED | UNICAST | MULTICAST_OFF
    _target_unicast(sim, listener)
    destinations = _spy(monkeypatch, sim)

    assert sim.events.button_press(0, 0) is True

    packet = await _recv(listener)
    assert destinations == [listener.getsockname()]
    assert (MULTICAST_GROUP, MULTICAST_PORT) not in destinations
    assert packet[-1] == checksum(packet[:-1])
    assert packet[12] == EventCode.BUTTON_PRESS


@pytest.mark.asyncio
async def test_multicast_only_skips_the_unicast_client(sim, listener, monkeypatch):
    """Unicast bit clear: the configured address is ignored even though it is set."""
    sim.world.event_mode = ENABLED
    _target_unicast(sim, listener)
    destinations = _spy(monkeypatch, sim)

    assert sim.events.button_press(0, 0) is True

    assert destinations == [(MULTICAST_GROUP, MULTICAST_PORT)]
    with pytest.raises(BlockingIOError):
        listener.recv(512)


@pytest.mark.asyncio
async def test_both_modes_deliver_twice(sim, listener, monkeypatch):
    sim.world.event_mode = ENABLED | UNICAST
    _target_unicast(sim, listener)
    destinations = _spy(monkeypatch, sim)

    assert sim.events.button_press(0, 0) is True

    await _recv(listener)
    assert destinations == [listener.getsockname(), (MULTICAST_GROUP, MULTICAST_PORT)]


@pytest.mark.asyncio
async def test_unicast_enabled_without_a_target_sends_nothing(sim, monkeypatch):
    """Unicast selected but no address configured: no datagram, and emit() reports
    that nothing was delivered."""
    sim.world.event_mode = ENABLED | UNICAST | MULTICAST_OFF
    sim.world.unicast_ip, sim.world.unicast_port = None, 0
    destinations = _spy(monkeypatch, sim)

    assert sim.events.button_press(0, 0) is False
    assert destinations == []


@pytest.mark.asyncio
async def test_events_disabled_suppresses_all_transports(sim, listener, monkeypatch):
    sim.world.event_mode = 0x00
    _target_unicast(sim, listener)
    destinations = _spy(monkeypatch, sim)

    assert sim.events.button_press(0, 0) is False
    assert destinations == []


@pytest.mark.asyncio
async def test_filtered_event_is_dropped_before_any_transport(sim, listener, monkeypatch):
    """A matching filter mutes the event on every transport, not just multicast."""
    from zencontrol_simulator.world import EventFilter

    sim.world.event_mode = ENABLED | 0x02 | UNICAST | MULTICAST_OFF
    _target_unicast(sim, listener)
    sim.world.event_filters.append(
        EventFilter(address=0xFF, instance=0xFF, mask=1 << int(EventCode.BUTTON_PRESS))
    )
    destinations = _spy(monkeypatch, sim)

    assert sim.events.button_press(0, 0) is False
    assert destinations == []
