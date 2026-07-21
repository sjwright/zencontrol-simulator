"""Command dispatch — only opcodes used by zencontrol-python / zencontrol-tpi."""

from __future__ import annotations

import logging
from typing import Callable, Optional

from .events import EventEmitter
from .protocol import ErrorCode, Request, ResponseType, build_response, command_name
from .world import (
    Colour,
    EventFilter,
    Light,
    World,
    bitmap_from_addresses,
    group_membership_bytes,
    int_to_be,
    scene_bitmask_bytes,
    signed_be16,
)

logger = logging.getLogger(__name__)

Handler = Callable[[Request], bytes]

# Opcodes actually exercised by zencontrol-python's interface layer
# (what zencontrol-tpi uses). Anything else returns UNKNOWN_CMD.
CMD = {
    "QUERY_GROUP_LABEL": 0x01,
    "QUERY_DALI_DEVICE_LABEL": 0x03,
    "QUERY_PROFILE_LABEL": 0x04,
    "QUERY_CURRENT_PROFILE_NUMBER": 0x05,
    "QUERY_TPI_EVENT_EMIT_STATE": 0x07,
    "ENABLE_TPI_EVENT_EMIT": 0x08,
    "QUERY_GROUP_NUMBERS": 0x09,
    "QUERY_PROFILE_NUMBERS": 0x0B,
    "QUERY_OCCUPANCY_INSTANCE_TIMERS": 0x0C,
    "QUERY_INSTANCES_BY_ADDRESS": 0x0D,
    "DALI_COLOUR": 0x0E,
    "QUERY_GROUP_MEMBERSHIP_BY_ADDRESS": 0x15,
    "QUERY_DALI_ADDRESSES_WITH_INSTANCES": 0x16,
    "QUERY_SCENE_NUMBERS_FOR_GROUP": 0x1A,
    "QUERY_SCENE_LABEL_FOR_GROUP": 0x1B,
    "QUERY_CONTROLLER_VERSION_NUMBER": 0x1C,
    "QUERY_CONTROL_GEAR_DALI_ADDRESSES": 0x1D,
    "QUERY_SCENE_LEVELS_BY_ADDRESS": 0x1E,
    "QUERY_CONTROLLER_LABEL": 0x24,
    "QUERY_IS_DALI_READY": 0x26,
    "QUERY_CONTROLLER_STARTUP_COMPLETE": 0x27,
    "DALI_ADD_TPI_EVENT_FILTER": 0x31,
    "QUERY_DALI_TPI_EVENT_FILTERS": 0x32,
    "DALI_CLEAR_TPI_EVENT_FILTERS": 0x33,
    "QUERY_DALI_COLOUR": 0x34,
    "QUERY_DALI_COLOUR_FEATURES": 0x35,
    "SET_SYSTEM_VARIABLE": 0x36,
    "QUERY_SYSTEM_VARIABLE": 0x37,
    "QUERY_DALI_COLOUR_TEMP_LIMITS": 0x38,
    "SET_TPI_EVENT_UNICAST_ADDRESS": 0x40,
    "QUERY_TPI_EVENT_UNICAST_ADDRESS": 0x41,
    "QUERY_SYSTEM_VARIABLE_NAME": 0x42,
    "QUERY_COLOUR_SCENE_MEMBERSHIP_BY_ADDR": 0x44,
    "QUERY_COLOUR_SCENE_0_7_DATA_FOR_ADDR": 0x45,
    "QUERY_COLOUR_SCENE_8_11_DATA_FOR_ADDR": 0x46,
    "DALI_INHIBIT": 0xA0,
    "DALI_SCENE": 0xA1,
    "DALI_ARC_LEVEL": 0xA2,
    "DALI_ON_STEP_UP": 0xA3,
    "DALI_STEP_DOWN_OFF": 0xA4,
    "DALI_UP": 0xA5,
    "DALI_DOWN": 0xA6,
    "DALI_RECALL_MAX": 0xA7,
    "DALI_RECALL_MIN": 0xA8,
    "DALI_OFF": 0xA9,
    "DALI_QUERY_LEVEL": 0xAA,
    "DALI_QUERY_CONTROL_GEAR_STATUS": 0xAB,
    "DALI_QUERY_CG_TYPE": 0xAC,
    "DALI_QUERY_LAST_SCENE": 0xAD,
    "DALI_QUERY_LAST_SCENE_IS_CURRENT": 0xAE,
    "DALI_ENABLE_DAPC_SEQ": 0xB2,
    "DALI_CUSTOM_FADE": 0xB4,
    "DALI_GO_TO_LAST_ACTIVE_LEVEL": 0xB5,
    "QUERY_DALI_INSTANCE_LABEL": 0xB7,
    "QUERY_DALI_SERIAL": 0xB9,
    "CHANGE_PROFILE_NUMBER": 0xC0,
    "DALI_STOP_FADE": 0xC1,
}

MAX_SCENE = 12
# PDF QUERY_SCENE_LEVELS_BY_ADDRESS returns all 16 DALI scene slots; unused = 0xFF.
SCENE_LEVEL_SLOTS = 16


def _ok(seq: int) -> bytes:
    return build_response(ResponseType.OK, seq)


def _answer(seq: int, data: bytes) -> bytes:
    return build_response(ResponseType.ANSWER, seq, data)


def _no_answer(seq: int) -> bytes:
    return build_response(ResponseType.NO_ANSWER, seq)


def _error(seq: int, code: ErrorCode) -> bytes:
    return build_response(ResponseType.ERROR, seq, bytes([int(code)]))


def _ascii(text: str) -> bytes:
    return text.encode("ascii", errors="replace")


def _label_answer(seq: int, text: str) -> bytes:
    """ANSWER with ASCII, or NO_ANSWER when empty (matches real controllers)."""
    if not text:
        return _no_answer(seq)
    return _answer(seq, _ascii(text))


class CommandDispatcher:
    def __init__(self, world: World, events: EventEmitter) -> None:
        self.world = world
        self.events = events
        self._handlers: dict[int, Handler] = {}
        self.request_count = 0
        self.error_count = 0
        self._register_all()

    def handle(self, request: Request) -> bytes:
        self.request_count += 1
        name = command_name(request.command, CMD)
        handler = self._handlers.get(request.command)
        if handler is None:
            self.error_count += 1
            logger.warning("Unsupported command %s (0x%02X)", name, request.command)
            return _error(request.seq, ErrorCode.UNKNOWN_CMD)
        try:
            response = handler(request)
            logger.debug(
                "%s seq=%s addr=%s -> 0x%02X len=%s",
                name,
                request.seq,
                request.data[0] if request.data else 0,
                response[0],
                response[2] if len(response) > 2 else 0,
            )
            if response[0] == ResponseType.ERROR:
                self.error_count += 1
            return response
        except Exception:
            self.error_count += 1
            logger.exception("Handler failed for %s", name)
            return _error(request.seq, ErrorCode.INVALID_ARGS)

    def _reg(self, opcode: int, handler: Handler) -> None:
        self._handlers[opcode] = handler

    def _register_all(self) -> None:
        w = self.world

        self._reg(CMD["QUERY_CONTROLLER_VERSION_NUMBER"],
                  lambda r: _answer(r.seq, bytes(w.version)))
        self._reg(CMD["QUERY_CONTROLLER_LABEL"],
                  lambda r: _label_answer(r.seq, w.label))
        self._reg(CMD["QUERY_CONTROLLER_STARTUP_COMPLETE"],
                  lambda r: _ok(r.seq) if w.startup_complete else _no_answer(r.seq))
        self._reg(CMD["QUERY_IS_DALI_READY"],
                  lambda r: _ok(r.seq) if w.dali_ready else _no_answer(r.seq))

        self._reg(CMD["QUERY_TPI_EVENT_EMIT_STATE"],
                  lambda r: _answer(r.seq, bytes([w.event_mode & 0xFF])))
        self._reg(CMD["ENABLE_TPI_EVENT_EMIT"], self._enable_events)
        self._reg(CMD["SET_TPI_EVENT_UNICAST_ADDRESS"], self._set_unicast)
        self._reg(CMD["QUERY_TPI_EVENT_UNICAST_ADDRESS"], self._query_unicast)
        self._reg(CMD["DALI_ADD_TPI_EVENT_FILTER"], self._add_filter)
        self._reg(CMD["DALI_CLEAR_TPI_EVENT_FILTERS"], self._clear_filters)
        self._reg(CMD["QUERY_DALI_TPI_EVENT_FILTERS"], self._query_filters)

        self._reg(CMD["QUERY_GROUP_NUMBERS"], self._query_group_numbers)
        self._reg(CMD["QUERY_GROUP_LABEL"], self._query_group_label)
        self._reg(CMD["QUERY_GROUP_MEMBERSHIP_BY_ADDRESS"], self._query_group_membership)
        self._reg(CMD["QUERY_SCENE_NUMBERS_FOR_GROUP"], self._query_scene_numbers_for_group)
        self._reg(CMD["QUERY_SCENE_LABEL_FOR_GROUP"], self._query_scene_label_for_group)

        self._reg(CMD["QUERY_PROFILE_NUMBERS"], self._query_profile_numbers)
        self._reg(CMD["QUERY_PROFILE_LABEL"], self._query_profile_label)
        self._reg(CMD["QUERY_CURRENT_PROFILE_NUMBER"],
                  lambda r: _answer(r.seq, int_to_be(w.current_profile, 2)))
        self._reg(CMD["CHANGE_PROFILE_NUMBER"], self._change_profile)

        self._reg(CMD["QUERY_CONTROL_GEAR_DALI_ADDRESSES"],
                  lambda r: _answer(r.seq, bitmap_from_addresses(list(w.lights.keys()))))
        self._reg(CMD["QUERY_DALI_DEVICE_LABEL"], self._query_device_label)
        self._reg(CMD["QUERY_DALI_SERIAL"], self._query_serial)
        self._reg(CMD["DALI_QUERY_LEVEL"], self._query_level)
        self._reg(CMD["DALI_QUERY_CG_TYPE"], self._query_cg_type)
        self._reg(CMD["DALI_QUERY_CONTROL_GEAR_STATUS"], self._query_cg_status)
        self._reg(CMD["DALI_QUERY_LAST_SCENE"], self._query_last_scene)
        self._reg(CMD["DALI_QUERY_LAST_SCENE_IS_CURRENT"], self._query_last_scene_current)
        self._reg(CMD["QUERY_DALI_COLOUR"], self._query_colour)
        self._reg(CMD["QUERY_DALI_COLOUR_FEATURES"], self._query_colour_features)
        self._reg(CMD["QUERY_DALI_COLOUR_TEMP_LIMITS"], self._query_colour_temp_limits)
        self._reg(CMD["QUERY_SCENE_LEVELS_BY_ADDRESS"], self._query_scene_levels)
        self._reg(CMD["QUERY_COLOUR_SCENE_MEMBERSHIP_BY_ADDR"], self._query_colour_scene_membership)
        self._reg(CMD["QUERY_COLOUR_SCENE_0_7_DATA_FOR_ADDR"],
                  lambda r: self._query_colour_scene_data(r, 0, 8))
        self._reg(CMD["QUERY_COLOUR_SCENE_8_11_DATA_FOR_ADDR"],
                  lambda r: self._query_colour_scene_data(r, 8, 12))

        self._reg(CMD["DALI_ARC_LEVEL"], self._arc_level)
        self._reg(CMD["DALI_OFF"], self._off)
        self._reg(CMD["DALI_RECALL_MAX"], self._recall_max)
        self._reg(CMD["DALI_RECALL_MIN"], self._recall_min)
        self._reg(CMD["DALI_ON_STEP_UP"], lambda r: self._step(r, +1, on_if_off=True))
        self._reg(CMD["DALI_STEP_DOWN_OFF"], lambda r: self._step(r, -1, off_at_min=True))
        self._reg(CMD["DALI_UP"], lambda r: self._step(r, +1))
        self._reg(CMD["DALI_DOWN"], lambda r: self._step(r, -1))
        self._reg(CMD["DALI_CUSTOM_FADE"], self._custom_fade)
        self._reg(CMD["DALI_GO_TO_LAST_ACTIVE_LEVEL"], self._go_last_active)
        self._reg(CMD["DALI_STOP_FADE"], self._stop_fade)
        self._reg(CMD["DALI_INHIBIT"], self._inhibit)
        self._reg(CMD["DALI_ENABLE_DAPC_SEQ"], lambda r: _no_answer(r.seq))
        self._reg(CMD["DALI_SCENE"], self._scene)
        self._reg(CMD["DALI_COLOUR"], self._dali_colour)

        self._reg(CMD["QUERY_DALI_ADDRESSES_WITH_INSTANCES"], self._query_addresses_with_instances)
        self._reg(CMD["QUERY_INSTANCES_BY_ADDRESS"], self._query_instances)
        self._reg(CMD["QUERY_DALI_INSTANCE_LABEL"], self._query_instance_label)
        self._reg(CMD["QUERY_OCCUPANCY_INSTANCE_TIMERS"], self._query_occupancy_timers)

        self._reg(CMD["SET_SYSTEM_VARIABLE"], self._set_sysvar)
        self._reg(CMD["QUERY_SYSTEM_VARIABLE"], self._query_sysvar)
        self._reg(CMD["QUERY_SYSTEM_VARIABLE_NAME"], self._query_sysvar_name)

    def _addr(self, request: Request) -> int:
        return request.data[0] if request.data else 0

    def _data(self, request: Request, index: int, default: int = 0) -> int:
        if index < len(request.data):
            return request.data[index]
        return default

    def _emit_level(self, wire: int, target_level: int, *, fading_seconds: int = 0) -> None:
        """Mutate world state and emit LEVEL_CHANGE_V2 for each affected target."""
        for target, previous, new in self.world.apply_level(
            wire, target_level, fading_seconds=fading_seconds
        ):
            self.events.level_change(target, previous, new)

    def _emit_per_light_level(self, wire: int, choose) -> None:
        for target, previous, new in self.world.apply_per_light_level(wire, choose):
            self.events.level_change(target, previous, new)

    def _unknown_target(self, seq: int) -> bytes:
        return _error(seq, ErrorCode.UNKNOWN_TARGET)

    def _check_level_target(self, seq: int, wire: int) -> bytes | None:
        if wire == 255:
            return None
        if wire <= 63:
            return None if self.world.light(wire) is not None else self._unknown_target(seq)
        if 64 <= wire <= 79:
            return None if self.world.group(wire - 64) is not None else self._unknown_target(seq)
        return self._unknown_target(seq)

    def _level_for_wire(self, wire: int) -> Optional[int]:
        if wire <= 63:
            light = self.world.light(wire)
            return light.visible_level() if light else None
        if 64 <= wire <= 79:
            return self.world.group_level(wire - 64)
        return None

    def _enable_events(self, request: Request) -> bytes:
        mode = self._addr(request)
        self.world.event_mode = mode
        return _answer(request.seq, bytes([mode & 0xFF]))

    def _set_unicast(self, request: Request) -> bytes:
        data = request.data
        if len(data) < 6:
            return _error(request.seq, ErrorCode.INVALID_ARGS)
        port = (data[0] << 8) | data[1]
        ip = f"{data[2]}.{data[3]}.{data[4]}.{data[5]}"
        if port == 0 or ip == "0.0.0.0":
            self.world.unicast_ip = None
            self.world.unicast_port = 0
        else:
            self.world.unicast_ip = ip
            self.world.unicast_port = port
        return _ok(request.seq)

    def _query_unicast(self, request: Request) -> bytes:
        port = self.world.unicast_port
        ip_parts = [0, 0, 0, 0]
        if self.world.unicast_ip:
            try:
                ip_parts = [int(x) for x in self.world.unicast_ip.split(".")]
            except ValueError:
                pass
        payload = bytes([
            self.world.event_mode & 0xFF,
            (port >> 8) & 0xFF,
            port & 0xFF,
            *ip_parts,
        ])
        return _answer(request.seq, payload)

    def _add_filter(self, request: Request) -> bytes:
        # PDF: successful add replies OK (empty).
        addr = self._addr(request)
        inst = self._data(request, 1, 0xFF)
        mask = (self._data(request, 2) << 8) | self._data(request, 3)
        for filt in self.world.event_filters:
            if filt.address == addr and filt.instance == inst:
                filt.mask |= mask
                return _ok(request.seq)
        self.world.event_filters.append(EventFilter(address=addr, instance=inst, mask=mask))
        return _ok(request.seq)

    def _clear_filters(self, request: Request) -> bytes:
        # PDF: OK when a matching filter is cleared; NO_ANSWER if none matched.
        addr = self._addr(request)
        inst = self._data(request, 1, 0xFF)
        unmask = (self._data(request, 2) << 8) | self._data(request, 3)
        remaining = []
        changed = False
        for filt in self.world.event_filters:
            if filt.address in (addr, 0xFF) or addr == 0xFF:
                if filt.instance in (inst, 0xFF) or inst == 0xFF:
                    new_mask = filt.mask & ~unmask & 0xFFFF
                    if new_mask != filt.mask:
                        changed = True
                    if new_mask:
                        remaining.append(EventFilter(
                            address=filt.address, instance=filt.instance, mask=new_mask
                        ))
                    continue
            remaining.append(filt)
        self.world.event_filters = remaining
        if not changed:
            return _no_answer(request.seq)
        return _ok(request.seq)

    def _query_filters(self, request: Request) -> bytes:
        start = self._data(request, 1, 0)
        inst_filter = self._data(request, 3, 0xFF)
        addr = self._addr(request)
        matches = [
            f for f in self.world.event_filters
            if (addr in (0xFF, f.address) or f.address == 0xFF)
            and (inst_filter in (0xFF, f.instance) or f.instance == 0xFF)
        ]
        page = matches[start : start + 15]
        out = bytearray([self.world.event_mode & 0xFF])
        for filt in page:
            out.append(filt.address & 0xFF)
            out.append(filt.instance & 0xFF)
            out.append((filt.mask >> 8) & 0xFF)
            out.append(filt.mask & 0xFF)
        if len(out) == 1:
            return _no_answer(request.seq)
        return _answer(request.seq, bytes(out))

    def _query_group_numbers(self, request: Request) -> bytes:
        nums = sorted(self.world.groups.keys())
        if not nums:
            return _no_answer(request.seq)
        return _answer(request.seq, bytes(nums))

    def _query_group_label(self, request: Request) -> bytes:
        group = self.world.group(self._addr(request))
        if group is None:
            return _no_answer(request.seq)
        return _label_answer(request.seq, group.label)

    def _query_group_membership(self, request: Request) -> bytes:
        light = self.world.light(self._addr(request))
        if light is None:
            return _no_answer(request.seq)
        return _answer(request.seq, group_membership_bytes(light.groups))

    def _query_scene_numbers_for_group(self, request: Request) -> bytes:
        group = self.world.group(self._addr(request))
        if group is None:
            return _no_answer(request.seq)
        return _answer(request.seq, scene_bitmask_bytes(group.scenes))

    def _query_scene_label_for_group(self, request: Request) -> bytes:
        group = self.world.group(self._addr(request))
        scene = self._data(request, 1)
        if group is None or scene not in group.scenes:
            return _no_answer(request.seq)
        return _label_answer(request.seq, group.scenes[scene])

    def _query_profile_numbers(self, request: Request) -> bytes:
        out = bytearray()
        for number in sorted(self.world.profiles.keys()):
            out.extend(int_to_be(number, 2))
        if not out:
            return _no_answer(request.seq)
        return _answer(request.seq, bytes(out))

    def _query_profile_label(self, request: Request) -> bytes:
        profile_id = (self._data(request, 2) << 8) | self._data(request, 3)
        profile = self.world.profiles.get(profile_id)
        if profile is None:
            return _no_answer(request.seq)
        return _label_answer(request.seq, profile.label)

    def _change_profile(self, request: Request) -> bytes:
        profile_id = (self._data(request, 2) << 8) | self._data(request, 3)
        if profile_id == 0xFFFF:
            profile_id = self.world.last_scheduled_profile
        elif profile_id not in self.world.profiles:
            return _error(request.seq, ErrorCode.INVALID_ARGS)
        self.world.current_profile = profile_id
        self.events.profile_change(profile_id)
        return _ok(request.seq)

    def _ecg_or_ecd(self, wire: int):
        if wire <= 63:
            return self.world.light(wire)
        if 64 <= wire <= 127:
            return self.world.device(wire - 64)
        return None

    def _query_device_label(self, request: Request) -> bytes:
        obj = self._ecg_or_ecd(self._addr(request))
        if obj is None:
            return _no_answer(request.seq)
        return _label_answer(request.seq, obj.label)

    def _query_serial(self, request: Request) -> bytes:
        obj = self._ecg_or_ecd(self._addr(request))
        if obj is None:
            return _no_answer(request.seq)
        return _answer(request.seq, int_to_be(obj.serial, 8))

    def _query_level(self, request: Request) -> bytes:
        level = self._level_for_wire(self._addr(request))
        if level is None:
            return _no_answer(request.seq)
        return _answer(request.seq, bytes([level & 0xFF]))

    def _query_cg_type(self, request: Request) -> bytes:
        light = self.world.light(self._addr(request))
        if light is None:
            return _no_answer(request.seq)
        return _answer(request.seq, bitmap_from_addresses(light.cg_types, max_bits=32))

    def _query_cg_status(self, request: Request) -> bytes:
        wire = self._addr(request)
        if wire <= 63:
            light = self.world.light(wire)
            if light is None:
                return _no_answer(request.seq)
            return _answer(request.seq, bytes([light.refresh_status()]))
        if 64 <= wire <= 79:
            level = self.world.group_level(wire - 64)
            if level is None:
                return _no_answer(request.seq)
            status = 0x04 if level not in (0, 255) else (0x00 if level == 0 else 0x04)
            # Aggregate fade_running from members
            if any(
                (m.refresh_status() & 0x10)
                for m in self.world.lights_in_group(wire - 64)
            ):
                status |= 0x10
            return _answer(request.seq, bytes([status]))
        if wire == 255:
            # PDF: broadcast status supported; OR member status bits.
            if not self.world.lights:
                return _no_answer(request.seq)
            status = 0
            any_on = False
            for light in self.world.lights.values():
                status |= light.refresh_status()
                if light.visible_level() > 0:
                    any_on = True
            if any_on:
                status |= 0x04
            else:
                status &= ~0x04
            return _answer(request.seq, bytes([status & 0xFF]))
        return _no_answer(request.seq)

    def _query_last_scene(self, request: Request) -> bytes:
        wire = self._addr(request)
        if wire <= 63:
            light = self.world.light(wire)
            if light is None:
                return _no_answer(request.seq)
            return _answer(request.seq, bytes([light.last_scene & 0xFF]))
        if 64 <= wire <= 79:
            group = self.world.group(wire - 64)
            if group is None:
                return _no_answer(request.seq)
            return _answer(request.seq, bytes([group.last_scene & 0xFF]))
        return _no_answer(request.seq)

    def _query_last_scene_current(self, request: Request) -> bytes:
        wire = self._addr(request)
        if wire <= 63:
            light = self.world.light(wire)
            if light is None:
                return _no_answer(request.seq)
            return _answer(request.seq, bytes([1 if light.last_scene_current else 0]))
        if 64 <= wire <= 79:
            group = self.world.group(wire - 64)
            if group is None:
                return _no_answer(request.seq)
            return _answer(request.seq, bytes([1 if group.last_scene_current else 0]))
        return _no_answer(request.seq)

    def _query_colour(self, request: Request) -> bytes:
        light = self.world.light(self._addr(request))
        if light is None or light.colour is None:
            return _no_answer(request.seq)
        raw = light.colour.to_bytes()
        if not raw:
            return _no_answer(request.seq)
        return _answer(request.seq, raw)

    def _query_colour_features(self, request: Request) -> bytes:
        light = self.world.light(self._addr(request))
        if light is None:
            return _no_answer(request.seq)
        return _answer(request.seq, bytes([light.colour_features.to_byte()]))

    def _query_colour_temp_limits(self, request: Request) -> bytes:
        light = self.world.light(self._addr(request))
        if light is None or light.colour_temp_limits is None:
            return _no_answer(request.seq)
        return _answer(request.seq, light.colour_temp_limits.to_bytes())

    def _query_scene_levels(self, request: Request) -> bytes:
        light = self.world.light(self._addr(request))
        if light is None:
            return _no_answer(request.seq)
        # PDF: all 16 DALI scene slots; 0xFF means not part of that scene.
        out = bytearray([0xFF] * SCENE_LEVEL_SLOTS)
        for i, level in enumerate(light.scene_levels[:MAX_SCENE]):
            if level is not None:
                out[i] = level & 0xFF
        return _answer(request.seq, bytes(out))

    def _query_colour_scene_membership(self, request: Request) -> bytes:
        light = self.world.light(self._addr(request))
        if light is None:
            return _no_answer(request.seq)
        scenes = [i for i, c in enumerate(light.scene_colours[:MAX_SCENE]) if c is not None]
        if not scenes:
            return _no_answer(request.seq)
        return _answer(request.seq, bytes(scenes))

    def _query_colour_scene_data(self, request: Request, start: int, end: int) -> bytes:
        light = self.world.light(self._addr(request))
        if light is None:
            return _no_answer(request.seq)
        out = bytearray()
        for i in range(start, end):
            colour = light.scene_colours[i] if i < len(light.scene_colours) else None
            if colour is None:
                # PDF: unused scene = type 0xFF + six 0xFF data bytes
                out.extend(bytes([0xFF] * 7))
            else:
                out.extend(colour.to_scene_blob())
        return _answer(request.seq, bytes(out))

    def _arc_level(self, request: Request) -> bytes:
        wire = self._addr(request)
        err = self._check_level_target(request.seq, wire)
        if err is not None:
            return err
        self._emit_level(wire, self._data(request, 3))
        return _ok(request.seq)

    def _off(self, request: Request) -> bytes:
        wire = self._addr(request)
        err = self._check_level_target(request.seq, wire)
        if err is not None:
            return err
        self._emit_level(wire, 0)
        return _ok(request.seq)

    def _recall_max(self, request: Request) -> bytes:
        wire = self._addr(request)
        err = self._check_level_target(request.seq, wire)
        if err is not None:
            return err
        self._emit_per_light_level(wire, lambda lt: lt.max_level)
        return _ok(request.seq)

    def _recall_min(self, request: Request) -> bytes:
        wire = self._addr(request)
        err = self._check_level_target(request.seq, wire)
        if err is not None:
            return err
        self._emit_per_light_level(wire, lambda lt: lt.min_level)
        return _ok(request.seq)

    def _step(self, request: Request, delta: int, on_if_off: bool = False, off_at_min: bool = False) -> bytes:
        wire = self._addr(request)
        err = self._check_level_target(request.seq, wire)
        if err is not None:
            return err

        def choose(lt: Light) -> int:
            current = lt.visible_level()
            # DALI UP must not ignite; only ON_STEP_UP does
            if current == 0:
                return max(1, lt.min_level) if on_if_off else 0
            # Only STEP_DOWN_OFF extinguishes at min
            if off_at_min and current <= lt.min_level:
                return 0
            if not off_at_min and current <= lt.min_level and delta < 0:
                return current
            target = current + delta
            if target <= 0:
                return lt.min_level if not off_at_min else 0
            return max(lt.min_level, min(lt.max_level, target))

        self._emit_per_light_level(wire, choose)
        return _ok(request.seq)

    def _custom_fade(self, request: Request) -> bytes:
        wire = self._addr(request)
        err = self._check_level_target(request.seq, wire)
        if err is not None:
            return err
        level = self._data(request, 1)
        fade_s = (self._data(request, 2) << 8) | self._data(request, 3)
        self._emit_level(wire, level, fading_seconds=fade_s)
        return _ok(request.seq)

    def _stop_fade(self, request: Request) -> bytes:
        wire = self._addr(request)
        err = self._check_level_target(request.seq, wire)
        if err is not None:
            return err
        self.world.clear_fade(wire)
        return _ok(request.seq)

    def _inhibit(self, request: Request) -> bytes:
        wire = self._addr(request)
        err = self._check_level_target(request.seq, wire)
        if err is not None:
            return err
        seconds = (self._data(request, 2) << 8) | self._data(request, 3)
        if not self.world.apply_inhibit(wire, seconds):
            return self._unknown_target(request.seq)
        return _ok(request.seq)

    def _go_last_active(self, request: Request) -> bytes:
        wire = self._addr(request)
        err = self._check_level_target(request.seq, wire)
        if err is not None:
            return err
        self._emit_per_light_level(
            wire, lambda lt: lt.last_active_level if lt.last_active_level else 254
        )
        return _ok(request.seq)

    def _scene(self, request: Request) -> bytes:
        wire = self._addr(request)
        err = self._check_level_target(request.seq, wire)
        if err is not None:
            return err
        scene = self._data(request, 3)
        if not 0 <= scene < MAX_SCENE:
            return _error(request.seq, ErrorCode.INVALID_ARGS)
        self.events.apply_and_emit_scene(wire, scene)
        return _ok(request.seq)

    def _dali_colour(self, request: Request) -> bytes:
        if len(request.data) < 3 or len(request.data) > 9:
            return _error(request.seq, ErrorCode.INVALID_ARGS)
        wire = request.data[0]
        err = self._check_level_target(request.seq, wire)
        if err is not None:
            return err
        level = request.data[1]
        colour_bytes = request.data[2:]
        colour = Colour.from_bytes(colour_bytes)
        if colour is None:
            return _error(request.seq, ErrorCode.INVALID_ARGS)
        for target in self.world.apply_colour(wire, colour):
            self.events.colour_change(target, colour.to_bytes())
        if level != 0xFF:
            self._emit_level(wire, level)
        return _ok(request.seq)

    def _query_addresses_with_instances(self, request: Request) -> bytes:
        start = self._data(request, 3, 0)
        addrs = sorted(64 + a for a in self.world.devices.keys())
        addrs = [a for a in addrs if a >= start][:60]
        if not addrs:
            return _no_answer(request.seq)
        return _answer(request.seq, bytes(addrs))

    def _query_instances(self, request: Request) -> bytes:
        wire = self._addr(request)
        if not 64 <= wire <= 127:
            return _no_answer(request.seq)
        device = self.world.device(wire - 64)
        if device is None or not device.instances:
            return _no_answer(request.seq)
        out = bytearray()
        for inst in device.instances:
            status = (0x01 if inst.error else 0) | (0x02 if inst.active else 0)
            out.extend([inst.number & 0xFF, inst.type & 0xFF, status & 0xFF, 0x00])
        return _answer(request.seq, bytes(out))

    def _query_instance_label(self, request: Request) -> bytes:
        wire = self._addr(request)
        inst_num = self._data(request, 3)
        if not 64 <= wire <= 127:
            return _no_answer(request.seq)
        inst = self.world.instance(wire - 64, inst_num)
        if inst is None:
            return _no_answer(request.seq)
        return _label_answer(request.seq, inst.label)

    def _query_occupancy_timers(self, request: Request) -> bytes:
        wire = self._addr(request)
        inst_num = self._data(request, 3)
        if not 64 <= wire <= 127:
            return _no_answer(request.seq)
        inst = self.world.instance(wire - 64, inst_num)
        if inst is None or inst.timers is None:
            return _no_answer(request.seq)
        t = inst.timers
        elapsed = t.seconds_since_detect()
        return _answer(request.seq, bytes([
            t.deadtime & 0xFF,
            t.hold & 0xFF,
            t.report & 0xFF,
            (elapsed >> 8) & 0xFF,
            elapsed & 0xFF,
        ]))

    def _set_sysvar(self, request: Request) -> bytes:
        var_id = self._addr(request)
        if not 0 <= var_id <= 147:
            return _error(request.seq, ErrorCode.INVALID_ARGS)
        var = self.world.system_variables.get(var_id)
        if var is None:
            # Don't invent unnamed vars — discovery is label-gated and gaps hide later IDs
            return _error(request.seq, ErrorCode.INVALID_ARGS)
        value = int.from_bytes(
            bytes([self._data(request, 2), self._data(request, 3)]), "big", signed=True
        )
        var.value = value
        self.events.system_variable_change(var_id, value, magnitude=0)
        return _ok(request.seq)

    def _query_sysvar(self, request: Request) -> bytes:
        var = self.world.system_variables.get(self._addr(request))
        if var is None:
            return _no_answer(request.seq)
        return _answer(request.seq, signed_be16(var.value))

    def _query_sysvar_name(self, request: Request) -> bytes:
        var = self.world.system_variables.get(self._addr(request))
        if var is None:
            return _no_answer(request.seq)
        return _label_answer(request.seq, var.name)
