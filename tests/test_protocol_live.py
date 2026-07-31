"""Live protocol-layer tests: zencontrol-python ↔ simulator."""

import asyncio
import time
from pathlib import Path

import pytest

from conftest import LEGACY_ACK

zencontrol = pytest.importorskip("zencontrol")

from zencontrol import (  # noqa: E402
    Transport,
    ZenRgbColour,
    ZenTcColour,
    ZenXyColour,
    ZenEventMask,
    ZenEventMode,
)

CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"


async def _wait(seconds: float = 0.15) -> None:
    await asyncio.sleep(seconds)


# ---------------------------------------------------------------------------
# Controller / readiness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_controller_version_and_label(live_protocol):
    p, c = live_protocol.protocol, live_protocol.controller
    version = await p.query_controller_version_number(c)
    assert version is not None
    assert "2" in version
    assert await p.query_controller_label(c) == "Simulator"
    assert await p.query_controller_startup_complete(c) is True
    assert await p.query_is_dali_ready(c) is True


@pytest.mark.asyncio
async def test_startup_delay_two_seconds_live():
    """Live: -s 2 equivalent — incomplete until 2s after sim start, then OK."""
    from zencontrol.testing import ZenTestClient
    from zencontrol_simulator.server import Simulator
    from zencontrol_simulator.world import load_world

    world = load_world(CONFIG)
    world.bind_host = "127.0.0.1"
    world.bind_port = 0
    world.heartbeat_interval = 0
    world.startup_complete = True
    world.startup_delay_s = 2.0
    # Leave started_at unset so Simulator.start() anchors the clock.

    sim = Simulator(world)
    await sim.start()
    assert sim._transport is not None
    port = sim._transport.get_extra_info("sockname")[1]
    mac = ":".join(f"{b:02x}" for b in world.mac)

    protocol = ZenTestClient(unicast=True, listen_ip="127.0.0.1", listen_port=0)
    controller = protocol.context.controller(
        id=1,
        name="sim",
        label="Sim",
        host="127.0.0.1",
        port=port,
        mac=mac,
    )
    protocol.set_controllers([controller])
    try:
        assert await protocol.query_controller_startup_complete(controller) is not True
        await _wait(2.1)
        assert await protocol.query_controller_startup_complete(controller) is True
    finally:
        await protocol.aclose()
        await sim.stop()


@pytest.mark.asyncio
async def test_simulate_response_latency_live():
    """Live: -t delays replies (~10ms default, ~100ms for instance labels)."""
    from zencontrol import ZenAddress, ZenAddressType, ZenInstance, ZenInstanceType
    from zencontrol.testing import ZenTestClient
    from zencontrol_simulator.server import Simulator
    from zencontrol_simulator.world import load_world

    world = load_world(CONFIG)
    world.bind_host = "127.0.0.1"
    world.bind_port = 0
    world.heartbeat_interval = 0
    world.simulate_response_latency = True

    sim = Simulator(world)
    await sim.start()
    assert sim._transport is not None
    port = sim._transport.get_extra_info("sockname")[1]
    mac = ":".join(f"{b:02x}" for b in world.mac)

    protocol = ZenTestClient(unicast=True, listen_ip="127.0.0.1", listen_port=0)
    controller = protocol.context.controller(
        id=1,
        name="sim",
        label="Sim",
        host="127.0.0.1",
        port=port,
        mac=mac,
    )
    protocol.set_controllers([controller])
    try:
        t0 = time.perf_counter()
        assert await protocol.query_controller_label(controller) == world.label
        fast_ms = (time.perf_counter() - t0) * 1000
        assert 8 <= fast_ms <= 80, f"default latency out of range: {fast_ms:.1f}ms"

        inst = ZenInstance(
            address=ZenAddress(
                controller=controller, type=ZenAddressType.ECD, number=0
            ),
            number=0,
            type=ZenInstanceType.PUSH_BUTTON,
        )
        t0 = time.perf_counter()
        label = await protocol.query_dali_instance_label(inst)
        slow_ms = (time.perf_counter() - t0) * 1000
        assert label == "On/Off"
        assert 80 <= slow_ms <= 250, f"instance-label latency out of range: {slow_ms:.1f}ms"
    finally:
        await protocol.aclose()
        await sim.stop()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_control_gear(live_protocol):
    p, c = live_protocol.protocol, live_protocol.controller
    gears = await p.query_control_gear_dali_addresses(c)
    numbers = sorted(a.number for a in gears)
    assert numbers == list(range(14))  # ECG 0–13 (incl. Living Fan / Theatre Blind)


@pytest.mark.asyncio
async def test_discover_groups(live_protocol):
    p, c = live_protocol.protocol, live_protocol.controller
    groups = await p.query_group_numbers(c)
    numbers = sorted(a.number for a in groups)
    assert numbers == [0, 1, 2, 3, 4, 5]
    assert await p.query_group_label(live_protocol.group(0)) == "Living Areas"
    assert await p.query_group_label(live_protocol.group(2)) == "Hallway North Wing"
    assert await p.query_group_label(live_protocol.group(4)) == "All Lights"

    live_protocol.world.lights[0].set_level(10)
    live_protocol.world.lights[1].set_level(77)
    info = await p.query_group_by_number(live_protocol.group(0))
    assert info == (0, True, 77)
    assert await p.query_group_by_number(live_protocol.group(15)) is None


@pytest.mark.asyncio
async def test_discover_devices_and_instances(live_protocol):
    p, c = live_protocol.protocol, live_protocol.controller
    devices = await p.query_dali_addresses_with_instances(c, start_address=0)
    ecd_nums = sorted(a.number for a in devices)
    assert 0 in ecd_nums and 1 in ecd_nums and 2 in ecd_nums
    assert 10 in ecd_nums and 11 in ecd_nums
    assert 13 in ecd_nums

    instances = await p.query_instances_by_address(live_protocol.ecd(0))
    types = {(i.number, i.type.value) for i in instances}
    assert (0, 1) in types  # push button
    assert (2, 3) in types  # occupancy

    entrance = await p.query_instances_by_address(live_protocol.ecd(2))
    assert len(entrance) == 6
    assert all(i.type.value == 1 for i in entrance)

    porch = await p.query_instances_by_address(live_protocol.ecd(10))
    porch_types = {i.type.value for i in porch}
    assert 3 in porch_types and 4 in porch_types  # occupancy + light sensor

    absolute = await p.query_instances_by_address(live_protocol.ecd(13))
    assert len(absolute) == 1
    assert absolute[0].type.value == 0x02
    assert await p.query_dali_instance_label(
        live_protocol.instance(13, 0, type_code=0x02)
    ) == "Slider"


@pytest.mark.asyncio
async def test_light_identity_and_features(live_protocol):
    from zencontrol import ZenCgType

    p = live_protocol.protocol
    ecg0 = live_protocol.ecg(0)
    ecg1 = live_protocol.ecg(1)
    ecg2 = live_protocol.ecg(2)

    assert await p.query_dali_device_label(ecg0) == "Living Room Ceiling"
    serial = await p.query_dali_serial(ecg0)
    assert serial is not None and serial > 0

    cg = await p.dali_query_cg_type(ecg0)
    assert cg is not None and ZenCgType.LED in cg and ZenCgType.COLOUR_CONTROL in cg

    features = await p.query_dali_colour_features(ecg0)
    assert features is not None
    assert features.supports_tunable

    rgb = await p.query_dali_colour_features(ecg2)
    assert rgb is not None

    limits = await p.query_dali_colour_temp_limits(ecg0)
    assert limits is not None
    assert limits.get("soft_warmest") == 2700 or limits.get("physical_warmest") == 2700

    groups = await p.query_group_membership_by_address(ecg1)
    assert {g.number for g in groups} == {0, 1}


# ---------------------------------------------------------------------------
# Level control + query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_arc_level_and_query(live_protocol):
    p = live_protocol.protocol
    addr = live_protocol.ecg(1)
    assert await p.dali_arc_level(addr, 77) is LEGACY_ACK
    assert await p.dali_query_level(addr) == 77
    assert live_protocol.world.lights[1].level == 77


@pytest.mark.asyncio
async def test_off_and_on_step_up(live_protocol):
    p = live_protocol.protocol
    addr = live_protocol.ecg(1)
    assert await p.dali_off(addr) is LEGACY_ACK
    assert await p.dali_query_level(addr) == 0
    assert await p.dali_up(addr) is LEGACY_ACK  # must not ignite
    assert await p.dali_query_level(addr) == 0
    assert await p.dali_on_step_up(addr) is LEGACY_ACK
    assert await p.dali_query_level(addr) == live_protocol.world.lights[1].min_level


@pytest.mark.asyncio
async def test_step_up_down_and_step_down_off(live_protocol):
    p = live_protocol.protocol
    addr = live_protocol.ecg(1)
    await p.dali_arc_level(addr, 10)
    assert await p.dali_up(addr) is LEGACY_ACK
    assert await p.dali_query_level(addr) == 11
    assert await p.dali_down(addr) is LEGACY_ACK
    assert await p.dali_query_level(addr) == 10

    await p.dali_arc_level(addr, 1)
    assert await p.dali_down(addr) is LEGACY_ACK  # stay at min
    assert await p.dali_query_level(addr) == 1
    assert await p.dali_step_down_off(addr) is LEGACY_ACK
    assert await p.dali_query_level(addr) == 0


@pytest.mark.asyncio
async def test_recall_max_min_and_last_active(live_protocol):
    p = live_protocol.protocol
    addr = live_protocol.ecg(1)
    live_protocol.world.lights[1].max_level = 200
    live_protocol.world.lights[1].min_level = 5

    assert await p.dali_recall_max(addr) is LEGACY_ACK
    assert await p.dali_query_level(addr) == 200
    assert await p.dali_recall_min(addr) is LEGACY_ACK
    assert await p.dali_query_level(addr) == 5

    await p.dali_arc_level(addr, 88)
    await p.dali_off(addr)
    assert await p.dali_go_to_last_active_level(addr) is LEGACY_ACK
    assert await p.dali_query_level(addr) == 88


@pytest.mark.asyncio
async def test_group_arc_and_mixed_query(live_protocol):
    p = live_protocol.protocol
    g0 = live_protocol.group(0)
    assert await p.dali_arc_level(g0, 40) is LEGACY_ACK
    assert await p.dali_query_level(g0) == 40
    assert live_protocol.world.lights[0].level == 40
    assert live_protocol.world.lights[1].level == 40

    await p.dali_arc_level(live_protocol.ecg(0), 10)
    await p.dali_arc_level(live_protocol.ecg(1), 20)
    assert await p.dali_query_level(g0) is None  # mixed → 255 → None


# ---------------------------------------------------------------------------
# Scenes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scene_recall_queries(live_protocol):
    p = live_protocol.protocol
    addr = live_protocol.ecg(0)
    assert await p.dali_scene(addr, 1) is LEGACY_ACK
    assert await p.dali_query_last_scene(addr) == 1
    assert await p.dali_query_last_scene_is_current(addr) is True
    assert await p.dali_query_level(addr) == 80  # config scene_levels[1]

    levels = await p.query_scene_levels_by_address(addr)
    assert levels[0] == 180
    assert levels[1] == 80

    scenes = await p.query_scene_numbers_by_address(addr)
    assert scenes == [0, 1, 2, 8, 9]

    await p.dali_arc_level(addr, 33)
    assert await p.dali_query_last_scene_is_current(addr) is False


@pytest.mark.asyncio
async def test_group_scene_and_labels(live_protocol):
    p = live_protocol.protocol
    g0 = live_protocol.group(0)
    scenes = await p.query_scene_numbers_for_group(g0)
    assert 0 in scenes and 1 in scenes
    assert await p.query_scene_label_for_group(g0, 1) == "Relax"

    assert await p.dali_scene(g0, 1) is LEGACY_ACK
    assert live_protocol.world.groups[0].last_scene == 1
    assert live_protocol.world.lights[0].level == 80
    assert live_protocol.world.lights[1].level == 100


# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_colour_tc_set_and_query(live_protocol):
    p = live_protocol.protocol
    addr = live_protocol.ecg(0)
    colour = ZenTcColour(kelvin=4000)
    assert await p.dali_colour(addr, colour) is True
    queried = await p.query_dali_colour(addr)
    assert queried is not None
    assert isinstance(queried, ZenTcColour)
    assert queried.kelvin == 4000
    assert live_protocol.world.lights[0].colour.kelvin == 4000


@pytest.mark.asyncio
async def test_colour_only_preserves_level_and_skips_level_event(live_protocol):
    """DALI_COLOUR with arc 0xFF changes colour only — no LEVEL_CHANGE_V2."""
    p = live_protocol.protocol
    addr = live_protocol.ecg(0)
    levels: list[int] = []
    colours: list = []

    async def on_level(*, address, arc_level, payload):
        if address.number == 0:
            levels.append(arc_level)

    async def on_colour(*, address, colour, payload):
        if address.number == 0:
            colours.append(colour)

    p.set_callbacks(level_change_callback=on_level, colour_change_callback=on_colour)
    await p.start_event_monitoring()
    await _wait(0.25)

    assert await p.dali_arc_level(addr, 77) is LEGACY_ACK
    await _wait(0.3)
    levels.clear()
    colours.clear()

    colour = ZenTcColour(kelvin=4200)
    assert await p.dali_colour(addr, colour, level=255) is True
    await _wait(0.35)

    assert await p.dali_query_level(addr) == 77
    assert live_protocol.world.lights[0].level == 77
    assert any(c is not None and c.kelvin == 4200 for c in colours)
    assert levels == []


@pytest.mark.asyncio
async def test_colour_rgb_set_and_query(live_protocol):
    p = live_protocol.protocol
    addr = live_protocol.ecg(2)
    colour = ZenRgbColour(r=10, g=20, b=30, w=0, a=0, f=0)
    assert await p.dali_colour(addr, colour, level=128) is True
    queried = await p.query_dali_colour(addr)
    assert queried is not None
    assert queried.r == 10 and queried.g == 20 and queried.b == 30
    assert await p.dali_query_level(addr) == 128


@pytest.mark.asyncio
async def test_colour_scene_data(live_protocol):
    p = live_protocol.protocol
    addr = live_protocol.ecg(0)
    membership = await p.query_colour_scene_membership_by_address(addr)
    assert 0 in membership and 1 in membership
    colours = await p.query_scene_colours_by_address(addr)
    assert colours[0] is not None
    assert colours[0].kelvin == 3000


# ---------------------------------------------------------------------------
# Profiles / sysvars
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_profiles(live_protocol):
    p, c = live_protocol.protocol, live_protocol.controller
    numbers = await p.query_profile_numbers(c)
    assert numbers is not None
    assert {1, 2, 3}.issubset(set(numbers))
    assert await p.query_profile_label(c, 2) == "Night"
    assert await p.query_current_profile_number(c) == 1
    assert await p.change_profile_number(c, 2) is True
    assert await p.query_current_profile_number(c) == 2
    assert live_protocol.world.current_profile == 2


@pytest.mark.asyncio
async def test_system_variables(live_protocol):
    p, c = live_protocol.protocol, live_protocol.controller
    assert await p.query_system_variable_name(c, 0) == "Demo Switch"
    assert await p.query_system_variable_name(c, 1) == "Demo Lux Sensor"
    assert await p.set_system_variable(c, 0, 9) is True
    assert await p.query_system_variable(c, 0) == 9
    assert live_protocol.world.system_variables[0].value == 9
    # Unknown IDs rejected — library soft-fails TPI ERROR as None
    assert await p.set_system_variable(c, 99, 1) is None
    assert 99 not in live_protocol.world.system_variables


# ---------------------------------------------------------------------------
# Occupancy timers / inhibit / fade
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_occupancy_timers(live_protocol):
    p = live_protocol.protocol
    inst = live_protocol.instance(0, 2, type_code=3)
    timers = await p.query_occupancy_instance_timers(inst)
    assert timers is not None
    assert timers.hold == 60
    assert timers.deadtime == 1
    assert isinstance(timers.last_detect, int)


@pytest.mark.asyncio
async def test_occupy_updates_last_detect_query(live_protocol):
    p = live_protocol.protocol
    inst = live_protocol.instance(0, 2, type_code=3)
    world_inst = live_protocol.world.instance(0, 2)
    assert world_inst is not None and world_inst.timers is not None
    world_inst.timers.last_motion_at = time.time() - 99

    assert live_protocol.sim.inject_occupancy(0, 2, occupied=True) is True
    timers = await p.query_occupancy_instance_timers(inst)
    assert timers is not None
    assert timers.last_detect <= 1


@pytest.mark.asyncio
async def test_occupy_without_note_motion_and_heartbeat_via_protocol(live_protocol):
    """PDF: instance OCCUPANCY is motion-only; unused payload byte is 0x01."""
    p = live_protocol.protocol
    occupied: list[tuple[int, int, bytes]] = []

    async def on_occ(*, instance, payload):
        occupied.append((instance.address.number, instance.number, bytes(payload)))

    p.set_callbacks(is_occupied_callback=on_occ)
    await p.start_event_monitoring()
    await _wait(0.25)

    world_inst = live_protocol.world.instance(0, 2)
    assert world_inst is not None and world_inst.timers is not None
    world_inst.timers.last_motion_at = time.time() - 99

    # occupied=False: still motion-shaped frame, does not advance last_detect.
    assert live_protocol.sim.inject_occupancy(0, 2, occupied=False) is True
    assert world_inst.timers.seconds_since_detect() >= 90
    assert live_protocol.sim.events.occupancy_heartbeat() is True
    await _wait(0.4)

    assert len(occupied) >= 2
    assert all(pl == bytes([2, 0x01]) for ecd, n, pl in occupied if ecd == 0 and n == 2)


@pytest.mark.asyncio
async def test_inhibit(live_protocol):
    p = live_protocol.protocol
    addr = live_protocol.ecg(1)
    assert await p.dali_inhibit(addr, 10) is True
    assert live_protocol.world.lights[1].is_inhibited() is True
    assert await p.dali_inhibit(addr, 0) is True
    assert live_protocol.world.lights[1].is_inhibited() is False


@pytest.mark.asyncio
async def test_custom_fade_and_stop(live_protocol):
    p = live_protocol.protocol
    addr = live_protocol.ecg(1)
    await p.dali_arc_level(addr, 0)
    assert await p.dali_custom_fade(addr, 100, 5) is True
    status = await p.dali_query_control_gear_status(addr)
    assert status is not None
    assert status.fade_running is True
    level = await p.dali_query_level(addr)
    assert level is not None
    assert 0 <= level <= 100
    assert await p.dali_stop_fade(addr) is True
    assert not (live_protocol.world.lights[1].status & 0x10)
    status2 = await p.dali_query_control_gear_status(addr)
    assert status2 is not None
    assert status2.fade_running is False


@pytest.mark.asyncio
async def test_dapc_sequence(live_protocol):
    p = live_protocol.protocol
    # PDF: DAPC replies NO_ANSWER (legacy); library maps that to None.
    assert await p.dali_enable_dapc_sequence(live_protocol.ecg(1)) is None


# ---------------------------------------------------------------------------
# Event emit / unicast / filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tpi_event_mode_and_unicast_roundtrip(live_protocol):
    p, c = live_protocol.protocol, live_protocol.controller
    assert await p.tpi_event_emit(
        c, ZenEventMode(enabled=True, filtering=False, transport=Transport.MULTICAST)
    ) is True
    assert await p.query_tpi_event_emit_state(c) is True

    await p.set_tpi_event_unicast_address(c, ipaddr="127.0.0.1", port=6970)
    info = await p.query_tpi_event_unicast_address(c)
    assert info is not None
    assert info.port == 6970
    assert info.ip == "127.0.0.1" or "127.0.0.1" in str(info)


@pytest.mark.asyncio
async def test_clear_tpi_event_unicast_address(live_protocol):
    """Sim extension: omit IP/port (zeros) clears unicast targeting."""
    p, c = live_protocol.protocol, live_protocol.controller
    await p.set_tpi_event_unicast_address(c, ipaddr="127.0.0.1", port=6970)
    await p.set_tpi_event_unicast_address(c)
    info = await p.query_tpi_event_unicast_address(c)
    assert info is not None
    assert info.port == 0
    assert info.ip == "0.0.0.0"
    assert live_protocol.world.unicast_ip is None
    assert live_protocol.world.unicast_port == 0


@pytest.mark.asyncio
async def test_event_filter_roundtrip(live_protocol):
    p = live_protocol.protocol
    addr = live_protocol.ecg(0)
    mask = ZenEventMask.LEVEL_CHANGE_V2
    assert await p.dali_add_tpi_event_filter(addr, mask) is True
    filters = await p.query_dali_tpi_event_filters(addr)
    assert filters  # at least one entry
    assert await p.dali_clear_tpi_event_filter(addr, mask) is True


@pytest.mark.asyncio
async def test_multicast_event_receipt():
    """Multicast client receives simulator events on 239.255.90.67:6969."""
    from zencontrol.testing import ZenTestClient
    from zencontrol_simulator.server import Simulator
    from zencontrol_simulator.world import load_world

    world = load_world(CONFIG)
    world.bind_host = "127.0.0.1"
    world.bind_port = 0
    world.heartbeat_interval = 0
    world.event_mode = 0x01  # enabled + multicast (unicast bit clear)
    sim = Simulator(world)
    await sim.start()
    assert sim._transport is not None
    port = sim._transport.get_extra_info("sockname")[1]
    mac = ":".join(f"{b:02x}" for b in world.mac)

    protocol = ZenTestClient(unicast=False)
    controller = protocol.context.controller(
        id=1,
        name="sim",
        label="Sim",
        host="127.0.0.1",
        port=port,
        mac=mac,
    )
    protocol.set_controllers([controller])
    buttons: list[tuple[int, int]] = []

    async def on_button(*, instance, payload):
        buttons.append((instance.address.number, instance.number))

    protocol.set_callbacks(button_press_callback=on_button)
    try:
        await protocol.start_event_monitoring()
        await _wait(0.3)
        assert sim.inject_button_press(0, 0) is True
        await _wait(0.4)
        assert (0, 0) in buttons
    finally:
        await protocol.aclose()
        await sim.stop()


@pytest.mark.asyncio
async def test_level_and_button_event_filters_mute_emit(live_protocol):
    """Filtering + TPI filters mute LEVEL_CHANGE_V2 and BUTTON_PRESS at the client."""
    p, c = live_protocol.protocol, live_protocol.controller
    levels: list[int] = []
    buttons: list[tuple[int, int]] = []

    async def on_level(*, address, arc_level, payload):
        levels.append(address.number)

    async def on_button(*, instance, payload):
        buttons.append((instance.address.number, instance.number))

    p.set_callbacks(level_change_callback=on_level, button_press_callback=on_button)
    await p.start_event_monitoring()
    await _wait(0.25)

    assert await p.tpi_event_emit(
        c,
        ZenEventMode(enabled=True, filtering=True, transport=Transport.UNICAST),
    ) is True
    assert await p.dali_add_tpi_event_filter(
        live_protocol.ecg(1), ZenEventMask.LEVEL_CHANGE_V2
    ) is True
    assert await p.dali_add_tpi_event_filter(
        live_protocol.instance(0, 0, type_code=1), ZenEventMask.BUTTON_PRESS
    ) is True

    assert await p.dali_arc_level(live_protocol.ecg(1), 40) is LEGACY_ACK
    assert live_protocol.world.lights[1].level == 40
    assert live_protocol.sim.inject_button_press(0, 0) is False
    await _wait(0.35)
    assert 1 not in levels
    assert buttons == []

    # Unfiltered ECG still delivers LEVEL_CHANGE_V2.
    assert await p.dali_arc_level(live_protocol.ecg(2), 33) is LEGACY_ACK
    await _wait(0.35)
    assert 2 in levels


# ---------------------------------------------------------------------------
# Live events via protocol callbacks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_level_change_event_via_protocol(live_protocol):
    p = live_protocol.protocol
    events: list = []

    async def on_level(*, address, arc_level, payload):
        events.append((address.number, arc_level, bytes(payload)))

    p.set_callbacks(level_change_callback=on_level)
    await p.start_event_monitoring()
    await _wait(0.25)

    assert await p.dali_arc_level(live_protocol.ecg(1), 55) is LEGACY_ACK
    await _wait(0.3)
    assert any(n == 1 and level == 55 for n, level, _ in events)


@pytest.mark.asyncio
async def test_scene_and_colour_events_via_protocol(live_protocol):
    p = live_protocol.protocol
    scenes: list = []
    colours: list = []

    async def on_scene(*, address, scene, active, payload):
        scenes.append((address.type.name, address.number, scene, active))

    async def on_colour(*, address, colour, payload):
        colours.append((address.number, colour))

    p.set_callbacks(scene_change_callback=on_scene, colour_change_callback=on_colour)
    await p.start_event_monitoring()
    await _wait(0.25)

    assert await p.dali_scene(live_protocol.ecg(0), 1) is LEGACY_ACK
    await _wait(0.3)
    assert any(t == "ECG" and n == 0 and s == 1 for t, n, s, _ in scenes)

    tc = ZenTcColour(kelvin=3500)
    assert await p.dali_colour(live_protocol.ecg(0), tc) is True
    await _wait(0.3)
    assert any(n == 0 and c is not None and c.kelvin == 3500 for n, c in colours)


@pytest.mark.asyncio
async def test_button_and_occupancy_inject_via_protocol(live_protocol):
    p = live_protocol.protocol
    buttons: list = []
    occupied: list = []

    async def on_button(*, instance, payload):
        buttons.append((instance.address.number, instance.number))

    async def on_hold(*, instance, payload):
        buttons.append(("hold", instance.address.number, instance.number))

    async def on_occ(*, instance, payload):
        occupied.append((instance.address.number, instance.number, bytes(payload)))

    p.set_callbacks(
        button_press_callback=on_button,
        button_hold_callback=on_hold,
        is_occupied_callback=on_occ,
    )
    await p.start_event_monitoring()
    await _wait(0.25)

    live_protocol.sim.inject_button_press(0, 0)
    live_protocol.sim.inject_button_hold(0, 1)
    live_protocol.sim.inject_occupancy(0, 2, occupied=True)
    await _wait(0.4)

    assert any(ecd == 0 and inst == 0 for ecd, inst in buttons if isinstance(ecd, int))
    assert any(item[0] == "hold" for item in buttons)
    assert any(ecd == 0 and inst == 2 for ecd, inst, _ in occupied)


@pytest.mark.asyncio
async def test_profile_and_sysvar_events_via_protocol(live_protocol):
    p, c = live_protocol.protocol, live_protocol.controller
    profiles: list = []
    sysvars: list = []

    async def on_profile(*, controller, profile, payload):
        profiles.append(profile)

    async def on_sysvar(*, controller, target, value, payload):
        sysvars.append((target, value))

    p.set_callbacks(
        profile_change_callback=on_profile,
        system_variable_change_callback=on_sysvar,
    )
    await p.start_event_monitoring()
    await _wait(0.25)

    assert await p.change_profile_number(c, 3) is True
    assert await p.set_system_variable(c, 1, 111) is True
    await _wait(0.3)
    assert 3 in profiles
    assert any(vid == 1 and val == 111 for vid, val in sysvars)


@pytest.mark.asyncio
async def test_group_level_event_via_protocol(live_protocol):
    p = live_protocol.protocol
    group_events: list = []

    async def on_level(*, address, arc_level, payload):
        pass  # required gate in zencontrol-python for group LEVEL_CHANGE_V2 dispatch

    async def on_group_level(*, address, arc_level, payload):
        group_events.append((address.number, arc_level))

    p.set_callbacks(
        level_change_callback=on_level,
        group_level_change_callback=on_group_level,
    )
    await p.start_event_monitoring()
    await _wait(0.25)

    assert await p.dali_arc_level(live_protocol.group(0), 44) is LEGACY_ACK
    await _wait(0.3)
    assert any(n == 0 and level == 44 for n, level in group_events)


# ---------------------------------------------------------------------------
# Labels, XY colour, scenes 8–11, broadcast, group queries, inject
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_instance_and_ecd_labels_live(live_protocol):
    p = live_protocol.protocol
    assert await p.query_dali_device_label(live_protocol.ecd(0)) == "Living Room Switch"
    assert await p.query_dali_device_label(live_protocol.ecd(1)) == "Kitchen Switch"
    assert await p.query_dali_instance_label(live_protocol.instance(0, 0)) == "On/Off"
    assert await p.query_dali_instance_label(live_protocol.instance(0, 2, type_code=3)) == "Motion"
    assert await p.query_dali_instance_label(live_protocol.instance(1, 0)) == "Kitchen Toggle"


@pytest.mark.asyncio
async def test_colour_xy_set_and_query(live_protocol):
    p = live_protocol.protocol
    addr = live_protocol.ecg(3)
    features = await p.query_dali_colour_features(addr)
    assert features is not None
    assert features.supports_xy is True

    colour = ZenXyColour(x=12345, y=23456)
    assert await p.dali_colour(addr, colour, level=90) is True
    queried = await p.query_dali_colour(addr)
    assert queried is not None
    assert isinstance(queried, ZenXyColour)
    assert queried.x == 12345 and queried.y == 23456
    assert await p.dali_query_level(addr) == 90
    assert live_protocol.world.lights[3].colour.x == 12345


@pytest.mark.asyncio
async def test_colour_scenes_include_8_11(live_protocol):
    p = live_protocol.protocol
    addr = live_protocol.ecg(0)
    membership = await p.query_colour_scene_membership_by_address(addr)
    assert 8 in membership and 9 in membership

    colours = await p.query_scene_colours_by_address(addr)
    assert len(colours) >= 10
    assert colours[8] is not None and colours[8].kelvin == 4500
    assert colours[9] is not None and colours[9].kelvin == 5500

    levels = await p.query_scene_levels_by_address(addr)
    assert levels[8] == 160
    assert levels[9] == 40

    assert await p.dali_scene(addr, 8) is LEGACY_ACK
    assert await p.dali_query_level(addr) == 160
    queried = await p.query_dali_colour(addr)
    assert queried is not None and queried.kelvin == 4500


@pytest.mark.asyncio
async def test_broadcast_arc_off_and_scene(live_protocol):
    p = live_protocol.protocol
    bcast = live_protocol.broadcast()
    assert await p.dali_arc_level(bcast, 33) is LEGACY_ACK
    assert all(lt.level == 33 for lt in live_protocol.world.lights.values())

    assert await p.dali_off(bcast) is LEGACY_ACK
    assert all(lt.level == 0 for lt in live_protocol.world.lights.values())

    assert await p.dali_scene(bcast, 0) is LEGACY_ACK
    assert live_protocol.world.lights[0].level == 180
    assert live_protocol.world.groups[0].last_scene == 0


@pytest.mark.asyncio
async def test_broadcast_colour_tc(live_protocol):
    p = live_protocol.protocol
    tc = ZenTcColour(kelvin=4200)
    assert await p.dali_colour(live_protocol.broadcast(), tc) is True
    assert live_protocol.world.lights[0].colour.kelvin == 4200
    assert live_protocol.world.lights[3].colour.type == "tc"
    assert live_protocol.world.lights[3].colour.kelvin == 4200


@pytest.mark.asyncio
async def test_group_last_scene_and_status(live_protocol):
    p = live_protocol.protocol
    g0 = live_protocol.group(0)
    assert await p.dali_scene(g0, 1) is LEGACY_ACK
    assert await p.dali_query_last_scene(g0) == 1
    assert await p.dali_query_last_scene_is_current(g0) is True

    await p.dali_arc_level(live_protocol.ecg(0), 10)
    assert await p.dali_query_last_scene_is_current(g0) is False

    await p.dali_arc_level(live_protocol.ecg(1), 0)
    assert await p.dali_custom_fade(live_protocol.ecg(1), 80, 5) is True
    status = await p.dali_query_control_gear_status(g0)
    assert status is not None
    assert status.fade_running is True


@pytest.mark.asyncio
async def test_startup_incomplete_and_dali_always_ready(live_protocol):
    p, c = live_protocol.protocol, live_protocol.controller
    live_protocol.world.startup_complete = False
    assert await p.query_controller_startup_complete(c) is not True
    live_protocol.world.startup_complete = True
    live_protocol.world.dali_ready = False  # ignored — simulator has no bus fault
    assert await p.query_is_dali_ready(c) is True


@pytest.mark.asyncio
async def test_empty_colour_membership_dimmer(live_protocol):
    p = live_protocol.protocol
    membership = await p.query_colour_scene_membership_by_address(live_protocol.ecg(1))
    assert membership == [] or membership is None


@pytest.mark.asyncio
async def test_xy_identity_and_serial(live_protocol):
    p = live_protocol.protocol
    addr = live_protocol.ecg(3)
    assert await p.query_dali_device_label(addr) == "XY Spotlight"
    serial = await p.query_dali_serial(addr)
    assert serial == 0x0100000000000004
    ean = await p.query_dali_ean(addr)
    assert ean == 10_000_000_000 + 3
    assert await p.query_dali_fitting_number(addr) == "1.3"
    assert await p.query_dali_fitting_number(live_protocol.ecd(0)) == "1.100"
    assert await p.query_dali_fitting_number(live_protocol.ecd(4)) == "1.104"
    assert await p.query_controller_fitting_number(live_protocol.controller) == "1"
    assert await p.query_dali_instance_fitting_number(
        live_protocol.instance(4, 2)
    ) == "1.104.2"
    groups = await p.query_group_membership_by_address(addr)
    assert groups == [] or groups is None or list(groups) == []


@pytest.mark.asyncio
async def test_inject_level_scene_colour_profile_events(live_protocol):
    from zencontrol_simulator.world import Colour

    p = live_protocol.protocol
    levels: list = []
    scenes: list = []
    colours: list = []
    profiles: list = []

    async def on_level(*, address, arc_level, payload):
        levels.append((address.number, arc_level))

    async def on_scene(*, address, scene, active, payload):
        scenes.append((address.number, scene))

    async def on_colour(*, address, colour, payload):
        colours.append((address.number, colour))

    async def on_profile(*, controller, profile, payload):
        profiles.append(profile)

    p.set_callbacks(
        level_change_callback=on_level,
        scene_change_callback=on_scene,
        colour_change_callback=on_colour,
        profile_change_callback=on_profile,
    )
    await p.start_event_monitoring()
    await _wait(0.25)

    live_protocol.sim.inject_level(1, 77)
    live_protocol.sim.inject_scene(0, 1)
    live_protocol.sim.inject_colour(3, Colour(type="xy", x=1111, y=2222))
    live_protocol.sim.inject_profile(3)
    await _wait(0.4)

    assert any(n == 1 and lv == 77 for n, lv in levels)
    assert any(n == 0 and s == 1 for n, s in scenes)
    assert any(n == 3 and c is not None and c.x == 1111 for n, c in colours)
    assert 3 in profiles


@pytest.mark.asyncio
async def test_group_inhibit_live(live_protocol):
    p = live_protocol.protocol
    g0 = live_protocol.group(0)
    assert await p.dali_inhibit(g0, 15) is True
    assert live_protocol.world.groups[0].is_inhibited() is True
    assert live_protocol.world.lights[0].is_inhibited() is True
    assert await p.dali_inhibit(g0, 0) is True
    assert live_protocol.world.groups[0].is_inhibited() is False


@pytest.mark.asyncio
async def test_fade_auto_complete_live(live_protocol):
    p = live_protocol.protocol
    addr = live_protocol.ecg(1)
    await p.dali_arc_level(addr, 0)
    assert await p.dali_custom_fade(addr, 50, 1) is True
    await _wait(1.2)
    status = await p.dali_query_control_gear_status(addr)
    assert status is not None
    assert status.fade_running is False
    assert await p.dali_query_level(addr) == 50


@pytest.mark.asyncio
async def test_custom_fade_receives_intermediate_and_final_events(live_protocol):
    """3s dali_custom_fade: client gets mid-fade LEVEL_CHANGE_V2 ticks and completion."""
    p = live_protocol.protocol
    addr = live_protocol.ecg(1)
    # (ecg, current, destination) from LEVEL_CHANGE_V2 payload
    events: list[tuple[int, int, int]] = []

    async def on_level(*, address, arc_level, payload):
        if len(payload) >= 2:
            events.append((address.number, payload[0], payload[1]))

    p.set_callbacks(level_change_callback=on_level)
    await p.start_event_monitoring()
    await _wait(0.25)

    await p.dali_arc_level(addr, 0)
    await _wait(0.3)
    events.clear()

    assert await p.dali_custom_fade(addr, 100, 3) is True
    # Fade progress loop ticks ~every 0.5s; wait past completion with slack.
    await _wait(3.8)

    mine = [(cur, dest) for n, cur, dest in events if n == 1]
    assert len(mine) >= 3
    assert any(0 < cur < 100 and dest == 100 for cur, dest in mine)
    assert any(cur == dest == 100 for cur, dest in mine)
    assert await p.dali_query_level(addr) == 100


@pytest.mark.asyncio
async def test_return_to_scheduled_profile(live_protocol):
    p, c = live_protocol.protocol, live_protocol.controller
    live_protocol.world.last_scheduled_profile = 1
    assert await p.change_profile_number(c, 3) is True
    assert await p.return_to_scheduled_profile(c) is True
    assert await p.query_current_profile_number(c) == 1


@pytest.mark.asyncio
async def test_sysvar_name_unknown_is_none(live_protocol):
    p, c = live_protocol.protocol, live_protocol.controller
    assert await p.query_system_variable_name(c, 99) is None
    assert await p.query_system_variable(c, 99) is None


@pytest.mark.asyncio
async def test_member_events_on_group_scene(live_protocol):
    p = live_protocol.protocol
    scenes: list = []
    levels: list = []

    async def on_scene(*, address, scene, active, payload):
        scenes.append((address.type.name, address.number, scene))

    async def on_level(*, address, arc_level, payload):
        levels.append((address.type.name, address.number, arc_level))

    p.set_callbacks(scene_change_callback=on_scene, level_change_callback=on_level)
    await p.start_event_monitoring()
    await _wait(0.25)

    assert await p.dali_scene(live_protocol.group(0), 1) is LEGACY_ACK
    await _wait(0.35)
    assert any(t == "GROUP" and n == 0 and s == 1 for t, n, s in scenes)
    assert any(t == "ECG" and n == 0 and s == 1 for t, n, s in scenes)
    assert any(t == "ECG" and n == 1 and s == 1 for t, n, s in scenes)
    assert any(t == "ECG" and n == 0 and lv == 80 for t, n, lv in levels)
    assert any(t == "ECG" and n == 1 and lv == 100 for t, n, lv in levels)
