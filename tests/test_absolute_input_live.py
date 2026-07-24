"""Live absolute-input tests: zencontrol-python ↔ simulator.

Covers protocol decode, interface entity model, simulator inject/world state,
and the ``_protocol.txt`` ABSOLUTE_INPUT_EVENT wire layout end-to-end.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

zencontrol = pytest.importorskip("zencontrol")

from zencontrol import (  # noqa: E402
    ZenAbsoluteInput,
    ZenControl,
    ZenEventMask,
    ZenInstanceType,
)


async def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 2.0,
    interval: float = 0.05,
    message: str = "condition not met",
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(message)


async def _wait(seconds: float = 0.15) -> None:
    await asyncio.sleep(seconds)


# ---------------------------------------------------------------------------
# Protocol layer (ZenProtocol + simulator)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_absolute_input_instance(live_protocol):
    """Demo ECD 13 is discoverable as instance type ABSOLUTE_INPUT (0x02)."""
    p = live_protocol.protocol
    devices = await p.query_dali_addresses_with_instances(
        live_protocol.controller, start_address=0
    )
    assert 13 in {a.number for a in devices}

    instances = await p.query_instances_by_address(live_protocol.ecd(13))
    assert len(instances) == 1
    assert instances[0].number == 0
    assert instances[0].type == ZenInstanceType.ABSOLUTE_INPUT
    assert instances[0].type.value == 0x02

    assert await p.query_dali_device_label(live_protocol.ecd(13)) == "Demo Absolute Input"
    assert (
        await p.query_dali_instance_label(
            live_protocol.instance(13, 0, type_code=0x02)
        )
        == "Slider"
    )


@pytest.mark.asyncio
async def test_absolute_input_inject_via_protocol_callback(live_protocol):
    """Simulator inject delivers the protocol payload the library expects."""
    p = live_protocol.protocol
    events: list[tuple[int, int, bytes, ZenInstanceType]] = []

    async def on_absolute(*, instance, payload):
        events.append(
            (
                instance.address.number,
                instance.number,
                bytes(payload),
                instance.type,
            )
        )

    p.set_callbacks(absolute_input_callback=on_absolute)
    await p.start_event_monitoring()
    await _wait(0.25)

    assert live_protocol.sim.inject_absolute_input(13, 0, 0x1234) is True
    await _wait_until(
        lambda: any(
            ecd == 13 and inst == 0 and payload == bytes([0, 0x12, 0x34])
            for ecd, inst, payload, _ in events
        ),
        message="expected absolute-input protocol callback for ECD 13",
    )
    assert events[0][3] == ZenInstanceType.ABSOLUTE_INPUT
    assert live_protocol.world.instance(13, 0).value == 0x1234


@pytest.mark.asyncio
async def test_absolute_input_protocol_txt_value_via_library(live_protocol):
    """``_protocol.txt`` example value 0xAABB arrives intact through the library."""
    p = live_protocol.protocol
    values: list[int] = []

    async def on_absolute(*, instance, payload):
        assert len(payload) >= 3
        values.append((payload[1] << 8) | payload[2])

    p.set_callbacks(absolute_input_callback=on_absolute)
    await p.start_event_monitoring()
    await _wait(0.25)

    live_protocol.sim.inject_absolute_input(13, 0, 0xAABB)
    await _wait_until(lambda: 0xAABB in values, message="expected 0xAABB value")
    assert live_protocol.world.instance(13, 0).value == 0xAABB


@pytest.mark.asyncio
async def test_absolute_input_inject_rejects_wrong_instance_type(live_protocol):
    with pytest.raises(ValueError, match="not an absolute input"):
        live_protocol.sim.inject_absolute_input(0, 0, 1)  # push button


@pytest.mark.asyncio
async def test_absolute_input_event_filter_mutes_emit(live_protocol):
    """DALI_ADD_TPI_EVENT_FILTER can mute ABSOLUTE_INPUT_EVENT for an ECD."""
    from zencontrol import ZenEventMode

    p = live_protocol.protocol
    c = live_protocol.controller
    events: list[bytes] = []

    async def on_absolute(*, instance, payload):
        events.append(bytes(payload))

    p.set_callbacks(absolute_input_callback=on_absolute)
    await p.start_event_monitoring()
    await _wait(0.25)

    # Filters are ignored unless DALI_EVENT_FILTERING is enabled in event mode.
    assert await p.tpi_event_emit(
        c,
        ZenEventMode(enabled=True, filtering=True, unicast=True, multicast=False),
    ) is True

    mask = ZenEventMask(absolute_input=True)
    assert await p.dali_add_tpi_event_filter(
        live_protocol.instance(13, 0, type_code=0x02), mask
    ) is True

    assert live_protocol.sim.inject_absolute_input(13, 0, 0x1111) is False
    await _wait(0.3)
    assert events == []
    # Inject still records the value on the world instance before emit is filtered.
    assert live_protocol.world.instance(13, 0).value == 0x1111


# ---------------------------------------------------------------------------
# Interface layer (ZenControl + ZenAbsoluteInput)
# ---------------------------------------------------------------------------


@pytest.fixture
async def live_zen_absolute():
    """Own simulator + ZenControl client (avoids sharing the live_protocol UDP client)."""
    from pathlib import Path

    from zencontrol_simulator.server import Simulator
    from zencontrol_simulator.world import load_world

    config = Path(__file__).resolve().parents[1] / "config.yaml"
    world = load_world(config)
    world.bind_host = "127.0.0.1"
    world.bind_port = 0
    world.heartbeat_interval = 0
    sim = Simulator(world)
    await sim.start()
    assert sim._transport is not None
    port = sim._transport.get_extra_info("sockname")[1]
    mac = ":".join(f"{b:02x}" for b in world.mac)

    zen = ZenControl(unicast=True)
    zen.add_controller(
        id=1,
        name="sim",
        label="Sim",
        host="127.0.0.1",
        port=port,
        mac=mac,
    )
    try:
        yield zen, sim, world
    finally:
        await zen.aclose()
        await sim.stop()


@pytest.mark.asyncio
async def test_get_absolute_inputs_interview_and_events(live_zen_absolute):
    """High-level discovery, interview fields, callback, singleton, and world sync."""
    zen, sim, world = live_zen_absolute
    ctrl = zen.controllers[0]
    await ctrl.interview()

    changes: list[tuple[ZenAbsoluteInput, int]] = []

    async def on_change(absolute_input: ZenAbsoluteInput, value: int) -> None:
        changes.append((absolute_input, value))

    found = await zen.get_absolute_inputs(controller=ctrl)
    assert len(found) >= 1
    slider = next(
        a
        for a in found
        if a.instance.address.number == 13 and a.instance.number == 0
    )
    assert slider.label == "Demo Absolute Input"
    assert slider.instance_label == "Slider"
    assert slider.value is None
    assert slider.instance.type == ZenInstanceType.ABSOLUTE_INPUT
    assert slider in ctrl.absolute_inputs

    blob = slider.interview_serialize()
    assert "Slider" in blob
    assert "Demo Absolute Input" in blob

    zen.absolute_input_change = on_change
    await zen.start()
    await _wait(0.25)

    sim.inject_absolute_input(13, 0, 0x1234)
    await _wait_until(
        lambda: any(value == 0x1234 for _, value in changes),
        message="expected absolute_input_change for 0x1234",
    )
    assert slider.value == 0x1234
    assert changes[0][0] is slider  # same singleton as discovery
    assert world.instance(13, 0).value == 0x1234

    # Unchanged value must not re-fire the high-level callback.
    before = len(changes)
    sim.inject_absolute_input(13, 0, 0x1234)
    await _wait(0.3)
    assert len(changes) == before

    sim.inject_absolute_input(13, 0, 0x0001)
    await _wait_until(
        lambda: any(value == 1 for _, value in changes),
        message="expected absolute_input_change for 1",
    )
    assert slider.value == 1
    assert changes[-1][0] is slider

    await zen.stop()


@pytest.mark.asyncio
async def test_absolute_input_event_before_discovery_creates_singleton(live_zen_absolute):
    """Events before get_absolute_inputs still create the registry singleton."""
    zen, sim, world = live_zen_absolute
    del world  # unused; kept for fixture symmetry

    changes: list[tuple[ZenAbsoluteInput, int]] = []

    async def on_change(absolute_input: ZenAbsoluteInput, value: int) -> None:
        changes.append((absolute_input, value))

    zen.absolute_input_change = on_change
    await zen.start()
    await _wait(0.25)

    sim.inject_absolute_input(13, 0, 0xABCD)
    await _wait_until(
        lambda: any(value == 0xABCD for _, value in changes),
        message="expected event before discovery",
    )
    early = changes[0][0]
    assert early.value == 0xABCD
    assert early.instance.address.number == 13

    found = await zen.get_absolute_inputs()
    slider = next(
        a
        for a in found
        if a.instance.address.number == 13 and a.instance.number == 0
    )
    assert slider is early
    assert slider.instance_label == "Slider"
    assert slider.value == 0xABCD

    await zen.stop()
