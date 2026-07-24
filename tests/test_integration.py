"""Live discovery smoke test against zencontrol-python (optional)."""

import asyncio
from pathlib import Path

import pytest

from zencontrol_simulator.server import Simulator
from zencontrol_simulator.world import load_world

CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"

zencontrol = pytest.importorskip("zencontrol")


@pytest.mark.asyncio
async def test_zencontrol_python_discovery_and_control():
    world = load_world(CONFIG)
    world.bind_host = "127.0.0.1"
    world.bind_port = 0
    world.heartbeat_interval = 0

    sim = Simulator(world)
    await sim.start()
    assert sim._transport is not None
    port = sim._transport.get_extra_info("sockname")[1]
    mac = ":".join(f"{b:02x}" for b in world.mac)

    button_events: list = []
    motion_events: list = []
    absolute_events: list = []
    profile_events: list = []
    sysvar_events: list = []
    colour_events: list = []

    try:
        async with zencontrol.ZenControl(unicast=True) as zen:
            zen.add_controller(
                id=1,
                name="sim",
                label="Sim",
                host="127.0.0.1",
                port=port,
                mac=mac,
            )
            ctrl = zen.controllers[0]
            assert await ctrl.is_controller_ready()
            await ctrl.interview()

            lights = await zen.get_lights()
            groups = await zen.get_groups()
            buttons = await zen.get_buttons()
            absolute_inputs = await zen.get_absolute_inputs()
            sensors = await zen.get_motion_sensors()
            profiles = await zen.get_profiles()
            sysvars = await zen.get_system_variables(give_up_after=5)

            assert len(lights) == 12
            assert len(groups) == 6
            assert len(buttons) >= 9
            assert len(absolute_inputs) >= 1
            assert len(sensors) >= 2
            assert len(profiles) == 3
            assert len(sysvars) >= 2

            by_addr = {lt.address.number: lt for lt in lights}
            assert by_addr[0].features.get("temperature") is True
            assert by_addr[1].features.get("brightness") is True
            assert by_addr[2].features.get("RGB") is True
            assert by_addr[3].features.get("brightness") is True
            assert by_addr[5].features.get("brightness") is True
            assert by_addr[7].features.get("temperature") is True
            # Instance labels reach button/sensor interview
            assert any(getattr(b, "instance_label", None) == "On/Off" for b in buttons)
            assert any(getattr(s, "instance_label", None) == "Motion" for s in sensors)
            assert any(getattr(b, "label", None) == "Living Room Switch" for b in buttons)
            assert any(getattr(b, "label", None) == "Entrance 6-Button" for b in buttons)
            assert any(getattr(s, "label", None) == "Porch Sensor" for s in sensors)
            slider = next(
                a
                for a in absolute_inputs
                if a.instance.address.number == 13 and a.instance.number == 0
            )
            assert slider.instance_label == "Slider"
            assert slider.value is None

            await zen.start()
            await asyncio.sleep(0.2)

            async def on_button(button):
                button_events.append(button)

            async def on_motion(sensor, occupied):
                motion_events.append((sensor, occupied))

            async def on_absolute(absolute_input, value):
                absolute_events.append((absolute_input, value))

            async def on_profile(profile):
                profile_events.append(profile)

            async def on_sysvar(system_variable, value, changed, by_me):
                sysvar_events.append((system_variable.id, value))

            async def on_light(*, light, level=None, colour=None, scene=None, **kwargs):
                if colour is not None:
                    colour_events.append((light.address.number, colour))

            zen.button_press = on_button
            zen.motion_event = on_motion
            zen.absolute_input_change = on_absolute
            zen.profile_change = on_profile
            zen.system_variable_change = on_sysvar
            zen.light_change = on_light

            # Arc level mutates + query matches
            light = by_addr[1]
            assert await light.set(level=50, fade=True) is True
            await asyncio.sleep(0.15)
            assert await zen.protocol.dali_query_level(light.address) == 50
            assert world.lights[1].level == 50

            # Tunable colour on ECG 0
            from zencontrol import ZenColour, ZenColourType

            tc = ZenColour(type=ZenColourType.TC, kelvin=4000)
            assert await by_addr[0].set(colour=tc) is True
            await asyncio.sleep(0.2)
            assert world.lights[0].colour is not None
            assert world.lights[0].colour.kelvin == 4000
            assert any(addr == 0 for addr, _ in colour_events)

            # Group scene recall
            group = next(g for g in groups if g.address.number == 0)
            assert await group.set_scene(1) is True
            await asyncio.sleep(0.15)
            assert world.groups[0].last_scene == 1
            assert world.lights[0].level == 80
            assert world.lights[1].level == 100

            # Profile switch
            assert await ctrl.switch_to_profile(2) is True
            await asyncio.sleep(0.2)
            assert world.current_profile == 2
            assert len(profile_events) >= 1

            # System variable set + event + query
            svar = next(v for v in sysvars if v.id == 0)
            await svar.set_value(42)
            await asyncio.sleep(0.2)
            assert world.system_variables[0].value == 42
            assert await zen.protocol.query_system_variable(ctrl, 0) == 42
            assert any(vid == 0 and val == 42 for vid, val in sysvar_events)

            # Injected events reach library callbacks
            sim.inject_button_press(0, 0)
            sim.inject_occupancy(0, 2, occupied=True)
            sim.inject_absolute_input(13, 0, 0x1234)
            await asyncio.sleep(0.4)
            assert len(button_events) >= 1
            assert len(motion_events) >= 1
            assert any(value == 0x1234 for _, value in absolute_events)
            assert absolute_events[0][0] is slider
            assert slider.value == 0x1234
    finally:
        await sim.stop()
