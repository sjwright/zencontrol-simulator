"""Shared fixtures for live simulator + zencontrol-python protocol tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from zencontrol_simulator.server import Simulator
from zencontrol_simulator.world import World, load_world

CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"


@dataclass(slots=True)
class LiveProtocol:
    """Running simulator paired with a zencontrol-python ZenProtocol client."""

    world: World
    sim: Simulator
    protocol: Any
    controller: Any

    def ecg(self, number: int):
        from zencontrol import ZenAddress, ZenAddressType

        return ZenAddress(
            controller=self.controller,
            type=ZenAddressType.ECG,
            number=number,
        )

    def group(self, number: int):
        from zencontrol import ZenAddress, ZenAddressType

        return ZenAddress(
            controller=self.controller,
            type=ZenAddressType.GROUP,
            number=number,
        )

    def ecd(self, number: int):
        from zencontrol import ZenAddress, ZenAddressType

        return ZenAddress(
            controller=self.controller,
            type=ZenAddressType.ECD,
            number=number,
        )

    def broadcast(self):
        from zencontrol import ZenAddress

        return ZenAddress.broadcast(self.controller)

    def instance(self, ecd: int, number: int, type_code: int = 1):
        from zencontrol import ZenInstance, ZenInstanceType

        return ZenInstance(
            address=self.ecd(ecd),
            number=number,
            type=ZenInstanceType(type_code),
        )


@pytest.fixture
async def live_protocol():
    """Start simulator on an ephemeral port and a ZenProtocol unicast client."""
    pytest.importorskip("zencontrol")
    from zencontrol import ZenProtocol
    from zencontrol.api.models import ZenController

    world = load_world(CONFIG)
    world.bind_host = "127.0.0.1"
    world.bind_port = 0
    world.heartbeat_interval = 0  # keep live tests free of background events

    sim = Simulator(world)
    await sim.start()
    assert sim._transport is not None
    port = sim._transport.get_extra_info("sockname")[1]
    mac = ":".join(f"{b:02x}" for b in world.mac)

    protocol = ZenProtocol(unicast=True, listen_ip="127.0.0.1", listen_port=0)
    controller = ZenController(
        id="1",
        name="sim",
        label="Sim",
        host="127.0.0.1",
        port=port,
        mac=mac,
        protocol=protocol,
    )
    protocol.set_controllers([controller])

    live = LiveProtocol(world=world, sim=sim, protocol=protocol, controller=controller)
    try:
        yield live
    finally:
        await protocol.aclose()
        await sim.stop()
