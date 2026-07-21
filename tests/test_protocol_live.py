"""Live protocol-layer tests: zencontrol-python ZenProtocol ↔ simulator."""

from __future__ import annotations

import asyncio

import pytest

zencontrol = pytest.importorskip("zencontrol")

from zencontrol import (  # noqa: E402
    ZenColour,
    ZenColourType,
    ZenEventMask,
    ZenEventMode,
)


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


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_control_gear(live_protocol):
    p, c = live_protocol.protocol, live_protocol.controller
    gears = await p.query_control_gear_dali_addresses(c)
    numbers = sorted(a.number for a in gears)
    assert numbers == [0, 1, 2]


@pytest.mark.asyncio
async def test_discover_groups(live_protocol):
    p, c = live_protocol.protocol, live_protocol.controller
    groups = await p.query_group_numbers(c)
    numbers = sorted(a.number for a in groups)
    assert numbers == [0, 1]
    assert await p.query_group_label(live_protocol.group(0)) == "Living Areas"


@pytest.mark.asyncio
async def test_discover_devices_and_instances(live_protocol):
    p, c = live_protocol.protocol, live_protocol.controller
    devices = await p.query_dali_addresses_with_instances(c, start_address=0)
    ecd_nums = sorted(a.number for a in devices)
    assert 0 in ecd_nums and 1 in ecd_nums

    instances = await p.query_instances_by_address(live_protocol.ecd(0))
    types = {(i.number, i.type.value) for i in instances}
    assert (0, 1) in types  # push button
    assert (2, 3) in types  # occupancy


@pytest.mark.asyncio
async def test_light_identity_and_features(live_protocol):
    p = live_protocol.protocol
    ecg0 = live_protocol.ecg(0)
    ecg1 = live_protocol.ecg(1)
    ecg2 = live_protocol.ecg(2)

    assert await p.query_dali_device_label(ecg0) == "Living Room Ceiling"
    serial = await p.query_dali_serial(ecg0)
    assert serial is not None and serial > 0

    cg = await p.dali_query_cg_type(ecg0)
    assert cg is not None and 6 in cg and 8 in cg

    features = await p.query_dali_colour_features(ecg0)
    assert features is not None
    assert features.get("supports_tunable") or features.get("colour_temperature")

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
    assert await p.dali_arc_level(addr, 77) is True
    assert await p.dali_query_level(addr) == 77
    assert live_protocol.world.lights[1].level == 77


@pytest.mark.asyncio
async def test_off_and_on_step_up(live_protocol):
    p = live_protocol.protocol
    addr = live_protocol.ecg(1)
    assert await p.dali_off(addr) is True
    assert await p.dali_query_level(addr) == 0
    assert await p.dali_up(addr) is True  # must not ignite
    assert await p.dali_query_level(addr) == 0
    assert await p.dali_on_step_up(addr) is True
    assert await p.dali_query_level(addr) == live_protocol.world.lights[1].min_level


@pytest.mark.asyncio
async def test_step_up_down_and_step_down_off(live_protocol):
    p = live_protocol.protocol
    addr = live_protocol.ecg(1)
    await p.dali_arc_level(addr, 10)
    assert await p.dali_up(addr) is True
    assert await p.dali_query_level(addr) == 11
    assert await p.dali_down(addr) is True
    assert await p.dali_query_level(addr) == 10

    await p.dali_arc_level(addr, 1)
    assert await p.dali_down(addr) is True  # stay at min
    assert await p.dali_query_level(addr) == 1
    assert await p.dali_step_down_off(addr) is True
    assert await p.dali_query_level(addr) == 0


@pytest.mark.asyncio
async def test_recall_max_min_and_last_active(live_protocol):
    p = live_protocol.protocol
    addr = live_protocol.ecg(1)
    live_protocol.world.lights[1].max_level = 200
    live_protocol.world.lights[1].min_level = 5

    assert await p.dali_recall_max(addr) is True
    assert await p.dali_query_level(addr) == 200
    assert await p.dali_recall_min(addr) is True
    assert await p.dali_query_level(addr) == 5

    await p.dali_arc_level(addr, 88)
    await p.dali_off(addr)
    assert await p.dali_go_to_last_active_level(addr) is True
    assert await p.dali_query_level(addr) == 88


@pytest.mark.asyncio
async def test_group_arc_and_mixed_query(live_protocol):
    p = live_protocol.protocol
    g0 = live_protocol.group(0)
    assert await p.dali_arc_level(g0, 40) is True
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
    assert await p.dali_scene(addr, 1) is True
    assert await p.dali_query_last_scene(addr) == 1
    assert await p.dali_query_last_scene_is_current(addr) is True
    assert await p.dali_query_level(addr) == 80  # config scene_levels[1]

    levels = await p.query_scene_levels_by_address(addr)
    assert levels[0] == 180
    assert levels[1] == 80

    await p.dali_arc_level(addr, 33)
    assert await p.dali_query_last_scene_is_current(addr) is False


@pytest.mark.asyncio
async def test_group_scene_and_labels(live_protocol):
    p = live_protocol.protocol
    g0 = live_protocol.group(0)
    scenes = await p.query_scene_numbers_for_group(g0)
    assert 0 in scenes and 1 in scenes
    assert await p.query_scene_label_for_group(g0, 1) == "Relax"

    assert await p.dali_scene(g0, 1) is True
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
    colour = ZenColour(type=ZenColourType.TC, kelvin=4000)
    assert await p.dali_colour(addr, colour) is True
    queried = await p.query_dali_colour(addr)
    assert queried is not None
    assert queried.type == ZenColourType.TC
    assert queried.kelvin == 4000
    assert live_protocol.world.lights[0].colour.kelvin == 4000


@pytest.mark.asyncio
async def test_colour_rgb_set_and_query(live_protocol):
    p = live_protocol.protocol
    addr = live_protocol.ecg(2)
    colour = ZenColour(type=ZenColourType.RGBWAF, r=10, g=20, b=30, w=0, a=0, f=0)
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
    # Unknown IDs rejected
    assert await p.set_system_variable(c, 99, 1) is not True


# ---------------------------------------------------------------------------
# Occupancy timers / inhibit / fade
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_occupancy_timers(live_protocol):
    p = live_protocol.protocol
    inst = live_protocol.instance(0, 2, type_code=3)
    timers = await p.query_occupancy_instance_timers(inst)
    assert timers is not None
    assert timers["hold"] == 60
    assert timers["deadtime"] == 1
    assert "last_detect" in timers


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
    p.cache.clear()  # status queries are cacheable
    status = await p.dali_query_control_gear_status(addr)
    assert status is not None
    assert status["fade_running"] is True
    level = await p.dali_query_level(addr)
    assert level is not None
    assert 0 <= level <= 100
    assert await p.dali_stop_fade(addr) is True
    assert not (live_protocol.world.lights[1].status & 0x10)
    p.cache.clear()
    status2 = await p.dali_query_control_gear_status(addr)
    assert status2 is not None
    assert status2["fade_running"] is False


@pytest.mark.asyncio
async def test_dapc_sequence(live_protocol):
    p = live_protocol.protocol
    assert await p.dali_enable_dapc_sequence(live_protocol.ecg(1)) is True


# ---------------------------------------------------------------------------
# Event emit / unicast / filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tpi_event_mode_and_unicast_roundtrip(live_protocol):
    p, c = live_protocol.protocol, live_protocol.controller
    assert await p.tpi_event_emit(
        c, ZenEventMode(enabled=True, filtering=False, unicast=False, multicast=True)
    ) is True
    assert await p.query_tpi_event_emit_state(c) is True

    await p.set_tpi_event_unicast_address(c, ipaddr="127.0.0.1", port=6970)
    info = await p.query_tpi_event_unicast_address(c)
    assert info is not None
    assert info["port"] == 6970
    assert info["ip"] == "127.0.0.1" or info.get("address") == "127.0.0.1" or "127.0.0.1" in str(info)


@pytest.mark.asyncio
async def test_event_filter_roundtrip(live_protocol):
    p = live_protocol.protocol
    addr = live_protocol.ecg(0)
    mask = ZenEventMask(level_change_v2=True)
    assert await p.dali_add_tpi_event_filter(addr, mask) is True
    filters = await p.query_dali_tpi_event_filters(addr)
    assert filters  # at least one entry
    assert await p.dali_clear_tpi_event_filter(addr, mask) is True


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

    assert await p.dali_arc_level(live_protocol.ecg(1), 55) is True
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

    assert await p.dali_scene(live_protocol.ecg(0), 1) is True
    await _wait(0.3)
    assert any(t == "ECG" and n == 0 and s == 1 for t, n, s, _ in scenes)

    tc = ZenColour(type=ZenColourType.TC, kelvin=3500)
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

    assert await p.dali_arc_level(live_protocol.group(0), 44) is True
    await _wait(0.3)
    assert any(n == 0 and level == 44 for n, level in group_events)
