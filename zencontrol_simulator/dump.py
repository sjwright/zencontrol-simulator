"""Dump a live Zencontrol controller into a simulator config YAML.

Read-only: queries labels, levels, colour, scenes, groups, ECDs/instances,
profiles, and system variables. Does not send control commands.

Example:
  zencontrol-dump -ip 1.2.3.4 -port 5108 -out config2.yaml
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path
from typing import Any

import yaml

LOGGER = logging.getLogger("zencontrol-dump")


def sanitize_controller_label(label: str) -> str:
    """Lowercase, non [a-z0-9] → space, trim, collapse spaces to hyphens."""
    text = label.lower()
    text = re.sub(r"[^a-z0-9]", " ", text)
    text = text.strip()
    text = re.sub(r" +", "-", text)
    return text or "controller"


def _hex_int(value: int) -> str:
    return f"0x{value:X}"


def _colour_dict(colour: Any) -> dict[str, Any] | None:
    from zencontrol import ZenColourType

    if colour is None or colour.type is None:
        return None
    if colour.type == ZenColourType.TC:
        return {"type": "tc", "kelvin": colour.kelvin}
    if colour.type == ZenColourType.RGBWAF:
        return {
            "type": "rgbwaf",
            "r": colour.r,
            "g": colour.g,
            "b": colour.b,
            "w": colour.w if colour.w is not None else 0,
            "a": colour.a if colour.a is not None else 0,
            "f": colour.f if colour.f is not None else 0,
        }
    if colour.type == ZenColourType.XY:
        return {"type": "xy", "x": colour.x, "y": colour.y}
    return None


def _scene_levels(levels: list[int | None] | None) -> list[int | None]:
    out: list[int | None] = [None] * 12
    if not levels:
        return out
    for i, level in enumerate(levels[:12]):
        out[i] = None if level is None else int(level)
    return out


def _scene_colours(colours: list[Any] | None) -> list[dict[str, Any] | None]:
    out: list[dict[str, Any] | None] = [None] * 12
    if not colours:
        return out
    for i, colour in enumerate(colours[:12]):
        out[i] = _colour_dict(colour)
    return out


async def _raw_byte(tpi: Any, controller: Any, command: int, address: int = 0) -> int | None:
    response = await tpi._send_basic(controller, command, address)
    if response and len(response) >= 1:
        return int(response[0])
    return None


async def dump_controller(tpi: Any, controller: Any) -> dict[str, Any]:
    from zencontrol import ZenInstanceType
    from zencontrol.api.types import Const

    instance_type_names = {
        ZenInstanceType.PUSH_BUTTON: "push_button",
        ZenInstanceType.ABSOLUTE_INPUT: "absolute_input",
        ZenInstanceType.OCCUPANCY_SENSOR: "occupancy_sensor",
        ZenInstanceType.LIGHT_SENSOR: "light_sensor",
        ZenInstanceType.GENERAL_SENSOR: "general_sensor",
    }

    LOGGER.info("Querying controller %s (%s:%s)", controller.mac, controller.host, controller.port)

    version = await tpi.query_controller_version_number(controller)
    version_parts = [2, 2, 0]
    if isinstance(version, str):
        try:
            version_parts = [int(x) for x in version.split(".")[:3]]
            while len(version_parts) < 3:
                version_parts.append(0)
        except ValueError:
            pass
    label = await tpi.query_controller_label(controller) or controller.label or "Controller"
    startup = await tpi.query_controller_startup_complete(controller)
    dali_ready = await tpi.query_is_dali_ready(controller)
    event_mode = await _raw_byte(tpi, controller, tpi.CMD["QUERY_TPI_EVENT_EMIT_STATE"])

    current_profile = await tpi.query_current_profile_number(controller)
    last_scheduled = current_profile or 0
    profile_info = await tpi.query_profile_information(controller)
    if profile_info:
        state, _profiles_detail = profile_info
        current_profile = int(state.get("current_active_profile", current_profile or 0))
        last_scheduled = int(state.get("last_scheduled_profile", last_scheduled))

    profile_numbers = await tpi.query_profile_numbers(controller) or []
    if current_profile is not None and current_profile not in profile_numbers:
        profile_numbers = sorted(set(profile_numbers) | {current_profile})
    profile_items = []
    for number in sorted(profile_numbers):
        plabel = await tpi.query_profile_label(controller, number)
        profile_items.append({
            "number": int(number),
            "label": plabel or f"Profile {number}",
        })
        LOGGER.info("  profile %s: %s", number, plabel)

    if controller.mac:
        mac = controller.mac.replace("-", ":").lower()
    else:
        mac = "00:00:00:00:00:00"
        LOGGER.warning("Controller MAC unknown — wrote %s; set controller.mac in the YAML if needed", mac)

    world: dict[str, Any] = {
        "controller": {
            "bind_host": "0.0.0.0",
            "bind_port": 5108,
            "mac": mac,
            "label": label,
            "version": version_parts,
            "startup_complete": bool(startup) if startup is not None else True,
            "dali_ready": bool(dali_ready) if dali_ready is not None else True,
            "event_mode": event_mode if event_mode is not None else 0x01,
            # Dump is a static snapshot — disable occupancy heartbeat by default.
            "heartbeat_interval": 0,
        },
        "lights": [],
        "groups": [],
        "devices": [],
        "profiles": {
            "current": int(current_profile or 0),
            "last_scheduled": int(last_scheduled or 0),
            "items": profile_items,
        },
        "system_variables": [],
    }

    # --- Groups (labels / scenes first so light membership can reference them) ---
    group_addrs = await tpi.query_group_numbers(controller) or []
    LOGGER.info("Groups: %d", len(group_addrs))
    for gaddr in sorted(group_addrs, key=lambda a: a.number):
        glabel = await tpi.query_group_label(gaddr)
        glevel = await tpi.dali_query_level(gaddr)
        scene_nums = await tpi.query_scene_numbers_for_group(gaddr) or []
        scenes: dict[int, str] = {}
        for scene in sorted(scene_nums):
            if not 0 <= scene <= 11:
                continue
            slabel = await tpi.query_scene_label_for_group(gaddr, scene)
            if slabel:
                scenes[int(scene)] = slabel
        world["groups"].append({
            "number": int(gaddr.number),
            "label": glabel or f"Group {gaddr.number}",
            "level": 0 if glevel is None else int(glevel),
            "scenes": scenes,
        })
        LOGGER.info("  group %s: %s (%d scenes)", gaddr.number, glabel, len(scenes))

    # --- Control gear / lights ---
    gears = await tpi.query_control_gear_dali_addresses(controller) or []
    LOGGER.info("Control gear: %d", len(gears))
    for addr in sorted(gears, key=lambda a: a.number):
        status_raw = await _raw_byte(
            tpi, controller, tpi.CMD["DALI_QUERY_CONTROL_GEAR_STATUS"], addr.ecg()
        )
        # Skip gear that does not answer status (absent / failed)
        if status_raw is None:
            LOGGER.warning("  ECG %s: no status — skipping", addr.number)
            continue

        dlabel = await tpi.query_dali_device_label(addr)
        serial = await tpi.query_dali_serial(addr)
        level = await tpi.dali_query_level(addr)
        min_level = await tpi.dali_query_min_level(addr)
        max_level = await tpi.dali_query_max_level(addr)
        last_scene = await tpi.dali_query_last_scene(addr)
        last_current = await tpi.dali_query_last_scene_is_current(addr)
        cg_types = await tpi.dali_query_cg_type(addr) or []
        groups = await tpi.query_group_membership_by_address(addr) or []
        group_nums = sorted({int(g.number) for g in groups})

        colour_features = {
            "supports_xy": False,
            "supports_tunable": False,
            "primary_count": 0,
            "rgbwaf_channels": 0,
        }
        colour = None
        colour_temp_limits = None
        if 8 in cg_types:
            features = await tpi.query_dali_colour_features(addr)
            if features:
                colour_features = {
                    "supports_xy": bool(features.get("supports_xy", False)),
                    "supports_tunable": bool(features.get("supports_tunable", False)),
                    "primary_count": int(features.get("primary_count", 0)),
                    "rgbwaf_channels": int(features.get("rgbwaf_channels", 0)),
                }
            colour = _colour_dict(await tpi.query_dali_colour(addr))
            if colour_features["supports_tunable"]:
                colour_temp_limits = await tpi.query_dali_colour_temp_limits(addr)

        scene_levels = _scene_levels(await tpi.query_scene_levels_by_address(addr))
        scene_colours = _scene_colours(await tpi.query_scene_colours_by_address(addr))

        light: dict[str, Any] = {
            "address": int(addr.number),
            "label": dlabel or f"Light {addr.number}",
            "serial": _hex_int(int(serial)) if serial else 0,
            "level": 0 if level is None else int(level),
            "min_level": 1 if min_level is None else int(min_level),
            "max_level": 254 if max_level is None else int(max_level),
            "last_scene": 0 if last_scene is None else int(last_scene),
            "last_scene_current": bool(last_current) if last_current is not None else False,
            "cg_types": [int(x) for x in cg_types],
            "colour": colour,
            "colour_features": colour_features,
            "groups": group_nums,
            "scene_levels": scene_levels,
            "status": status_raw,
        }
        if colour_temp_limits:
            light["colour_temp_limits"] = {
                "physical_warmest": int(colour_temp_limits["physical_warmest"]),
                "physical_coolest": int(colour_temp_limits["physical_coolest"]),
                "soft_warmest": int(colour_temp_limits["soft_warmest"]),
                "soft_coolest": int(colour_temp_limits["soft_coolest"]),
                "step_value": int(colour_temp_limits["step_value"]),
            }
        if any(c is not None for c in scene_colours):
            light["scene_colours"] = scene_colours

        world["lights"].append(light)
        LOGGER.info(
            "  ECG %s: %s level=%s groups=%s cg=%s",
            addr.number,
            dlabel,
            light["level"],
            group_nums,
            cg_types,
        )

    # --- ECDs / instances ---
    ecds = await tpi.query_dali_addresses_with_instances(controller, 0) or []
    LOGGER.info("Devices with instances: %d", len(ecds))
    for ecd in sorted(ecds, key=lambda a: a.number):
        dlabel = await tpi.query_dali_device_label(ecd)
        serial = await tpi.query_dali_serial(ecd)
        instances = await tpi.query_instances_by_address(ecd) or []
        inst_list: list[dict[str, Any]] = []
        for inst in sorted(instances, key=lambda i: i.number):
            if inst.type is None:
                continue
            type_name = instance_type_names.get(inst.type)
            if type_name is None:
                LOGGER.warning(
                    "  ECD %s inst %s: unsupported type %s — skipped",
                    ecd.number,
                    inst.number,
                    inst.type,
                )
                continue
            ilabel = await tpi.query_dali_instance_label(inst)
            entry: dict[str, Any] = {
                "number": int(inst.number),
                "type": type_name,
                "label": ilabel or f"Instance {inst.number}",
                "active": True,
                "error": False,
            }
            if inst.type == ZenInstanceType.OCCUPANCY_SENSOR:
                timers = await tpi.query_occupancy_instance_timers(inst)
                if timers:
                    entry["timers"] = {
                        "deadtime": int(timers["deadtime"]),
                        "hold": int(timers["hold"]),
                        "report": int(timers["report"]),
                        "last_detect": int(timers["last_detect"]),
                    }
                else:
                    entry["timers"] = {
                        "deadtime": 1,
                        "hold": 60,
                        "report": 20,
                        "last_detect": 0,
                    }
            inst_list.append(entry)
            LOGGER.info(
                "  ECD %s.%s %s (%s)",
                ecd.number,
                inst.number,
                type_name,
                ilabel,
            )
        if not inst_list:
            continue
        world["devices"].append({
            "address": int(ecd.number),
            "label": dlabel or f"Device {ecd.number}",
            "serial": _hex_int(int(serial)) if serial else 0,
            "instances": inst_list,
        })

    # --- System variables (stop after consecutive unnamed IDs) ---
    give_up_after = 10
    failed = 0
    LOGGER.info("System variables…")
    for vid in range(Const.MAX_SYSVAR):
        name = await tpi.query_system_variable_name(controller, vid)
        if not name:
            failed += 1
            if failed >= give_up_after:
                break
            continue
        failed = 0
        value = await tpi.query_system_variable(controller, vid)
        world["system_variables"].append({
            "id": int(vid),
            "name": name,
            "value": 0 if value is None else int(value),
        })
        LOGGER.info("  sysvar %s: %s = %s", vid, name, value)

    return world


class _HexInt(int):
    """YAML representable int that dumps as 0x…."""


def _represent_hex_int(dumper: yaml.Dumper, data: _HexInt) -> Any:
    return dumper.represent_scalar("tag:yaml.org,2002:int", f"0x{int(data):X}")


def _prepare_for_yaml(obj: Any) -> Any:
    """Convert serial hex strings back to ints tagged for hex dump where useful."""
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if key == "serial" and isinstance(value, str) and value.startswith("0x"):
                out[key] = _HexInt(int(value, 0))
            elif key == "event_mode" and isinstance(value, int):
                out[key] = _HexInt(value)
            elif key == "status" and isinstance(value, int):
                out[key] = _HexInt(value)
            else:
                out[key] = _prepare_for_yaml(value)
        return out
    if isinstance(obj, list):
        return [_prepare_for_yaml(x) for x in obj]
    return obj


def write_yaml(path: Path, world: dict[str, Any]) -> None:
    class Dumper(yaml.SafeDumper):
        pass

    Dumper.add_representer(_HexInt, _represent_hex_int)
    payload = _prepare_for_yaml(world)
    header = (
        "# Auto-generated by zencontrol-dump\n"
        "# Read-only snapshot of a live Zencontrol controller for zencontrol-simulator.\n"
        "# Control commands mutate in-memory state only; nothing is written back to disk.\n\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(header)
        yaml.dump(
            payload,
            fh,
            Dumper=Dumper,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
            width=100,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dump a live Zencontrol controller to a zencontrol-simulator YAML world",
    )
    parser.add_argument(
        "-ip",
        "--ip",
        required=True,
        help="Controller IP address or hostname",
    )
    parser.add_argument(
        "-port",
        "--port",
        type=int,
        default=5108,
        help="Controller TPI port (default: 5108)",
    )
    parser.add_argument(
        "-out",
        "--out",
        type=Path,
        default=None,
        help='Output YAML path (default: config-{controller-label}.yaml)',
    )
    parser.add_argument(
        "-mac",
        "--mac",
        default=None,
        help="Controller MAC for the dumped world (default: 00:00:00:00:00:00)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Debug logging",
    )
    return parser


async def _dump(args: argparse.Namespace) -> None:
    from zencontrol import ZenController, ZenProtocol

    async with ZenProtocol(print_traffic=False) as tpi:
        ctrl = ZenController(
            protocol=tpi,
            id="1",
            name="dump",
            label="",
            host=args.ip,
            port=args.port,
            mac=args.mac,
        )
        tpi.set_controllers([ctrl])

        ready = await tpi.query_controller_startup_complete(ctrl)
        if ready is False:
            LOGGER.warning("Controller reports startup incomplete — continuing anyway")
        dali = await tpi.query_is_dali_ready(ctrl)
        if dali is False:
            LOGGER.warning("DALI bus not ready — continuing anyway")

        world = await dump_controller(tpi, ctrl)

    label = world["controller"]["label"]
    out = args.out or Path(f"config-{sanitize_controller_label(label)}.yaml")
    write_yaml(out, world)
    LOGGER.info(
        "Wrote %s (%d lights, %d groups, %d devices, %d profiles, %d sysvars)",
        out,
        len(world["lights"]),
        len(world["groups"]),
        len(world["devices"]),
        len(world["profiles"]["items"]),
        len(world["system_variables"]),
    )


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        from zencontrol import run_with_keyboard_interrupt
    except ImportError:
        LOGGER.error(
            "zencontrol-python is required for zencontrol-dump "
            "(pip install -e ../zencontrol-python)"
        )
        sys.exit(1)

    run_with_keyboard_interrupt(lambda: _dump(args))


if __name__ == "__main__":
    main()
