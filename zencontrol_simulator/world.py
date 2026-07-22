"""Controller world loaded from YAML config with mutable runtime state."""

from __future__ import annotations

import logging
import math
import time
from copy import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from .protocol import mac_from_string


INSTANCE_TYPES = {
    "push_button": 0x01,
    "absolute_input": 0x02,
    "occupancy_sensor": 0x03,
    "light_sensor": 0x04,
    "general_purpose_sensor": 0x06,
    "general_sensor": 0x06,
    1: 0x01,
    2: 0x02,
    3: 0x03,
    4: 0x04,
    6: 0x06,
}

# How often simulated system variables are refreshed from the daylight sine.
SYSVAR_SIMULATE_INTERVAL = 30.0
# Real controllers re-emit LEVEL_CHANGE_V2 ~every 500ms during long fades.
FADE_PROGRESS_INTERVAL_S = 0.5
FADE_PROGRESS_MIN_MS = 2000
# Signed BE16 range used by QUERY/SET_SYSTEM_VARIABLE.
_SYSVAR_VALUE_MAX = 32767
# Control-gear status bits (IEC 62386 / zencontrol status byte).
_STATUS_LAMP_ON = 0x04
_STATUS_FADE_RUNNING = 0x10


def _set_inhibit_until(holder: Any, seconds: int) -> None:
    if seconds <= 0:
        holder.inhibited_until = None
    else:
        holder.inhibited_until = time.time() + seconds


def _is_inhibited_until(holder: Any) -> bool:
    until = holder.inhibited_until
    if until is None:
        return False
    if time.time() >= until:
        holder.inhibited_until = None
        return False
    return True


def destination_level(light: "Light") -> int:
    """Arc destination after a command (fade target while fading, else current)."""
    return light.fade_to if light.fade_to is not None else light.level


def scene_colour_bytes(light: "Light", scene: int) -> Optional[bytes]:
    """COLOUR_CHANGE payload when this scene defines colour data on the light."""
    if (
        0 <= scene < len(light.scene_colours)
        and light.scene_colours[scene] is not None
        and light.colour is not None
    ):
        return light.colour.to_bytes()
    return None


@dataclass
class SceneEffects:
    """Companion TPI events implied by a scene recall.

    ECG wires always get SCENE + LEVEL (+ COLOUR when the scene has colour).
    Group wires get SCENE always; LEVEL/COLOUR only when members agree.
    Parent groups of an ECG-only recall are never included (invalidate only).
    """

    scenes: list[int] = field(default_factory=list)
    levels: list[tuple[int, int, int]] = field(default_factory=list)
    colours: list[tuple[int, bytes]] = field(default_factory=list)

    def add_ecg(self, light: "Light", scene: int, prev_level: int) -> None:
        """Record SCENE/LEVEL/COLOUR for one control gear after it was mutated."""
        wire = light.address
        self.scenes.append(wire)
        self.levels.append((wire, prev_level, destination_level(light)))
        blob = scene_colour_bytes(light, scene)
        if blob is not None:
            self.colours.append((wire, blob))


@dataclass
class Colour:
    type: str  # tc | rgbwaf | xy
    kelvin: Optional[int] = None
    r: Optional[int] = None
    g: Optional[int] = None
    b: Optional[int] = None
    w: Optional[int] = None
    a: Optional[int] = None
    f: Optional[int] = None
    x: Optional[int] = None
    y: Optional[int] = None

    @classmethod
    def from_bytes(cls, data: bytes) -> Optional["Colour"]:
        if not data:
            return None
        kind = data[0]
        if kind == 0x20 and len(data) >= 3:
            return cls(type="tc", kelvin=(data[1] << 8) | data[2])
        if kind == 0x80 and len(data) >= 7:
            return cls(
                type="rgbwaf",
                r=data[1], g=data[2], b=data[3],
                w=data[4], a=data[5], f=data[6],
            )
        if kind == 0x10 and len(data) >= 5:
            return cls(
                type="xy",
                x=(data[1] << 8) | data[2],
                y=(data[3] << 8) | data[4],
            )
        return None

    def to_bytes(self) -> bytes:
        if self.type == "tc":
            k = self.kelvin or 3000
            return bytes([0x20, (k >> 8) & 0xFF, k & 0xFF])
        if self.type == "rgbwaf":
            return bytes([
                0x80,
                self.r or 0,
                self.g or 0,
                self.b or 0,
                self.w or 0,
                self.a or 0,
                self.f or 0,
            ])
        if self.type == "xy":
            x = self.x or 0
            y = self.y or 0
            return bytes([0x10, (x >> 8) & 0xFF, x & 0xFF, (y >> 8) & 0xFF, y & 0xFF])
        return b""

    def to_scene_blob(self) -> bytes:
        """Pad/truncate to 7 bytes for colour scene queries (PDF: unused bytes 0xFF)."""
        raw = self.to_bytes()
        if len(raw) >= 7:
            return raw[:7]
        return raw + bytes([0xFF] * (7 - len(raw)))


@dataclass
class ColourFeatures:
    supports_xy: bool = False
    supports_tunable: bool = False
    primary_count: int = 0
    rgbwaf_channels: int = 0

    def to_byte(self) -> int:
        value = 0
        if self.supports_xy:
            value |= 0x01
        if self.supports_tunable:
            value |= 0x02
        value |= (self.primary_count & 0x07) << 2
        value |= (self.rgbwaf_channels & 0x07) << 5
        return value


@dataclass
class ColourTempLimits:
    physical_warmest: int = 2700
    physical_coolest: int = 6500
    soft_warmest: int = 2700
    soft_coolest: int = 6500
    step_value: int = 1

    def to_bytes(self) -> bytes:
        vals = [
            self.physical_warmest,
            self.physical_coolest,
            self.soft_warmest,
            self.soft_coolest,
            self.step_value,
        ]
        out = bytearray()
        for v in vals:
            out.extend([(v >> 8) & 0xFF, v & 0xFF])
        return bytes(out)


@dataclass
class Light:
    address: int
    label: str
    serial: int = 0
    level: int = 0
    min_level: int = 1
    max_level: int = 254
    last_active_level: int = 254
    last_scene: int = 0
    last_scene_current: bool = False
    cg_types: list[int] = field(default_factory=list)
    colour: Optional[Colour] = None
    colour_features: ColourFeatures = field(default_factory=ColourFeatures)
    colour_temp_limits: Optional[ColourTempLimits] = None
    groups: list[int] = field(default_factory=list)
    scene_levels: list[Optional[int]] = field(default_factory=lambda: [None] * 12)
    scene_colours: list[Optional[Colour]] = field(default_factory=lambda: [None] * 12)
    status: int = 0x00
    fading_until: Optional[float] = None
    fade_from: Optional[int] = None
    fade_to: Optional[int] = None
    fade_started_at: Optional[float] = None
    fade_origin: Optional[int] = None  # wire that started the custom fade
    inhibited_until: Optional[float] = None

    def _set_lamp_on(self, on: bool) -> None:
        if on:
            self.status |= _STATUS_LAMP_ON
        else:
            self.status &= ~_STATUS_LAMP_ON

    def _clear_fade_fields(self) -> None:
        self.fading_until = None
        self.fade_from = None
        self.fade_to = None
        self.fade_started_at = None
        self.fade_origin = None
        self.status &= ~_STATUS_FADE_RUNNING

    def clear_fade(self, *, freeze: bool = False) -> None:
        if freeze and self.fading_until is not None:
            self.level = self.visible_level(expire=False)
            self._set_lamp_on(self.level > 0)
        self._clear_fade_fields()

    def _expire_fade_if_due(self) -> None:
        if self.fading_until is not None and time.time() >= self.fading_until:
            if self.fade_to is not None:
                self.level = self.fade_to
            self._clear_fade_fields()
            self._set_lamp_on(self.level > 0)

    def visible_level(self, *, expire: bool = True) -> int:
        """Level as seen on QUERY — interpolated while a custom fade is running."""
        if expire:
            self._expire_fade_if_due()
        if (
            self.fading_until is None
            or self.fade_from is None
            or self.fade_to is None
            or self.fade_started_at is None
        ):
            return self.level
        if time.time() >= self.fading_until:
            return self.fade_to
        total = self.fading_until - self.fade_started_at
        if total <= 0:
            return self.fade_to
        t = min(1.0, max(0.0, (time.time() - self.fade_started_at) / total))
        return int(round(self.fade_from + (self.fade_to - self.fade_from) * t))

    def refresh_status(self) -> int:
        """Expire fade/inhibit timers and return current status byte."""
        self._expire_fade_if_due()
        if self.inhibited_until is not None and time.time() >= self.inhibited_until:
            self.inhibited_until = None
        # lamp_power_on follows visible level while a fade is in progress
        if self.fading_until is not None:
            self._set_lamp_on(self.visible_level(expire=False) > 0)
        return self.status & 0xFF

    def set_level(
        self,
        level: int,
        *,
        fading_seconds: float = 0,
        fade_origin: Optional[int] = None,
    ) -> None:
        level = max(0, min(254, int(level)))
        from_level = self.visible_level()
        if level > 0:
            self.last_active_level = level
        self.level = level
        self.last_scene_current = False
        if fading_seconds > 0:
            now = time.time()
            self.fade_from = from_level
            self.fade_to = level
            self.fade_started_at = now
            self.fading_until = now + float(fading_seconds)
            self.fade_origin = fade_origin
            self.status |= _STATUS_FADE_RUNNING
            # Keep lamp_power_on aligned with currently visible light during fade
            self._set_lamp_on(from_level > 0)
        else:
            self.clear_fade()
            self._set_lamp_on(level > 0)

    def set_colour(self, colour: Colour) -> None:
        self.colour = colour
        self.last_scene_current = False

    def set_inhibit(self, seconds: int) -> None:
        _set_inhibit_until(self, seconds)

    def is_inhibited(self) -> bool:
        return _is_inhibited_until(self)

    def apply_scene(
        self,
        scene: int,
        *,
        fading_seconds: float = 0,
        fade_origin: Optional[int] = None,
    ) -> None:
        self.last_scene = scene & 0xFF
        self.last_scene_current = True
        if 0 <= scene < len(self.scene_levels) and self.scene_levels[scene] is not None:
            self.set_level(
                self.scene_levels[scene] or 0,
                fading_seconds=fading_seconds,
                fade_origin=fade_origin,
            )
            self.last_scene_current = True  # set_level cleared it
        if 0 <= scene < len(self.scene_colours) and self.scene_colours[scene] is not None:
            self.colour = copy(self.scene_colours[scene])
            self.last_scene_current = True


@dataclass
class Group:
    number: int
    label: str
    level: int = 0
    last_scene: int = 0
    last_scene_current: bool = False
    scenes: dict[int, str] = field(default_factory=dict)
    inhibited_until: Optional[float] = None

    def set_level(self, level: int) -> None:
        self.level = max(0, min(254, int(level)))
        self.last_scene_current = False

    def set_inhibit(self, seconds: int) -> None:
        _set_inhibit_until(self, seconds)

    def is_inhibited(self) -> bool:
        return _is_inhibited_until(self)


@dataclass
class InstanceTimers:
    """Occupancy timers. Wire `last_detect` is seconds since last motion."""

    deadtime: int = 1
    hold: int = 60
    report: int = 20
    last_motion_at: float = field(default_factory=time.time)

    def seconds_since_detect(self) -> int:
        return min(65535, max(0, int(time.time() - self.last_motion_at)))

    def note_motion(self) -> None:
        self.last_motion_at = time.time()


@dataclass
class Instance:
    number: int
    type: int
    label: str = ""
    timers: Optional[InstanceTimers] = None
    active: bool = True
    error: bool = False


@dataclass
class Device:
    address: int
    label: str
    serial: int = 0
    instances: list[Instance] = field(default_factory=list)


@dataclass
class Profile:
    number: int
    label: str


@dataclass
class SystemVariable:
    id: int
    name: str
    value: int = 0
    # If set, value tracks a daylight sine (0 at midnight → simulate at midday).
    simulate: Optional[int] = None


def daylight_sine_value(maximum: int, *, seconds_since_midnight: Optional[float] = None) -> int:
    """Map local time of day onto ``[0, maximum]`` with a raised cosine.

    Midnight → 0, midday → *maximum*, next midnight → 0.
    """
    if maximum < 0:
        raise ValueError(f"simulate maximum must be >= 0, got {maximum}")
    if seconds_since_midnight is None:
        lt = time.localtime()
        seconds_since_midnight = (
            lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec + time.time() % 1
        )
    phase = 2.0 * math.pi * (seconds_since_midnight % 86400.0) / 86400.0
    return int(round(maximum * (1.0 - math.cos(phase)) / 2.0))


@dataclass
class EventFilter:
    address: int
    instance: int
    mask: int


@dataclass
class World:
    """YAML-backed controller world with mutable runtime TPI state."""

    bind_host: str
    bind_port: int
    mac: bytes
    label: str
    version: tuple[int, int, int]
    startup_complete: bool
    dali_ready: bool
    lights: dict[int, Light]
    groups: dict[int, Group]
    devices: dict[int, Device]
    profiles: dict[int, Profile]
    current_profile: int
    last_scheduled_profile: int
    system_variables: dict[int, SystemVariable]
    event_mode: int = 0x01
    unicast_ip: Optional[str] = None
    unicast_port: int = 0
    event_filters: list[EventFilter] = field(default_factory=list)
    # Emit IS_OCCUPIED (0x06) multicast keepalive for discovery; 0 disables.
    heartbeat_interval: float = 5.0
    heartbeat_ecd: Optional[int] = None
    heartbeat_instance: Optional[int] = None
    # Default fade for scene recalls (milliseconds). Real controllers re-emit
    # LEVEL_CHANGE_V2 every ~500ms while fading when this is greater than 2000.
    dim_time_ms: int = 0
    # First segment of fitting numbers (PDF QUERY_CONTROLLER_FITTING_NUMBER / defaults).
    fitting_number: str = "1"

    def light(self, address: int) -> Optional[Light]:
        return self.lights.get(address)

    def group(self, number: int) -> Optional[Group]:
        return self.groups.get(number)

    def device(self, address: int) -> Optional[Device]:
        return self.devices.get(address)

    def instance(self, ecd: int, number: int) -> Optional[Instance]:
        device = self.devices.get(ecd)
        if device is None:
            return None
        for inst in device.instances:
            if inst.number == number:
                return inst
        return None

    def first_occupancy(self) -> Optional[tuple[int, int]]:
        """Return (ecd, instance) for the first occupancy sensor, if any."""
        for ecd in sorted(self.devices):
            for inst in self.devices[ecd].instances:
                if inst.type == 0x03:
                    return (ecd, inst.number)
        return None

    def heartbeat_target(self) -> Optional[tuple[int, int]]:
        """Resolve configured or first occupancy sensor for the heartbeat."""
        if self.heartbeat_ecd is not None and self.heartbeat_instance is not None:
            inst = self.instance(self.heartbeat_ecd, self.heartbeat_instance)
            if inst is not None and inst.type == 0x03:
                return (self.heartbeat_ecd, self.heartbeat_instance)
            return None
        return self.first_occupancy()

    def lights_in_group(self, group_number: int) -> list[Light]:
        return [lt for lt in self.lights.values() if group_number in lt.groups]

    def group_level(self, group_number: int) -> Optional[int]:
        """Return group arc level, or 255 if members disagree (mixed)."""
        members = self.lights_in_group(group_number)
        group = self.groups.get(group_number)
        if not members:
            return group.level if group else None
        levels = {m.visible_level() for m in members}
        if len(levels) == 1:
            return next(iter(levels))
        return 255

    def agreed_member_colour(self, group_number: int) -> Optional[Colour]:
        """Return member colour when all coloured members agree; else None."""
        colours = [
            m.colour for m in self.lights_in_group(group_number)
            if m.colour is not None
        ]
        if not colours:
            return None
        blobs = {c.to_bytes() for c in colours}
        if len(blobs) != 1:
            return None
        return colours[0]

    def agreed_group_level(self, group_number: int) -> Optional[int]:
        """Member arc when unanimous; None if empty/missing/mixed (255)."""
        level = self.group_level(group_number)
        if level is None or level == 255:
            return None
        return level

    def event_prev_for_group(self, group: Group) -> int:
        """LEVEL_CHANGE_V2 current for a group wire (stored level when mixed)."""
        level = self.group_level(group.number)
        if level is None or level == 255:
            return group.level
        return level

    def sync_group_level(
        self, group: Group, *, clear_scene_if_mixed: bool = False
    ) -> Optional[int]:
        """Store agreed member level on the group; optionally clear scene-current if mixed."""
        level = self.agreed_group_level(group.number)
        if level is not None:
            group.level = level
            return level
        if clear_scene_if_mixed:
            group.last_scene_current = False
        return None

    def invalidate_groups_sharing(
        self, lights: list[Light], *, except_group: Optional[int] = None
    ) -> None:
        """Clear last_scene_current on groups that share these lights (siblings too)."""
        seen: set[int] = set()
        for light in lights:
            for gnum in light.groups:
                if gnum == except_group or gnum in seen:
                    continue
                seen.add(gnum)
                group = self.groups.get(gnum)
                if group is None:
                    continue
                group.last_scene_current = False
                self.sync_group_level(group)

    def invalidate_parent_groups(self, light: Light) -> None:
        """ECG level/colour/scene changes clear parent group scene-current and sync level."""
        self.invalidate_groups_sharing([light])

    def apply_level(
        self, wire: int, level: int, *, fading_seconds: int = 0
    ) -> list[tuple[int, int, int]]:
        """Apply level; return list of (wire, previous, new) for events."""
        level = max(0, min(254, int(level)))
        changes: list[tuple[int, int, int]] = []
        if wire == 255:
            for light in self.lights.values():
                prev = light.visible_level()
                light.set_level(level, fading_seconds=fading_seconds, fade_origin=wire)
                changes.append((light.address, prev, light.level))
            for group in self.groups.values():
                prev = group.level
                group.set_level(level)
                changes.append((64 + group.number, prev, group.level))
            return changes
        if wire <= 63:
            light = self.lights.get(wire)
            if light is None:
                return []
            prev = light.visible_level()
            light.set_level(level, fading_seconds=fading_seconds, fade_origin=wire)
            self.invalidate_parent_groups(light)
            changes.append((wire, prev, light.level))
            return changes
        if 64 <= wire <= 79:
            group_num = wire - 64
            group = self.groups.get(group_num)
            if group is None:
                return []
            event_prev = self.event_prev_for_group(group)
            group.set_level(level)
            members = self.lights_in_group(group_num)
            for light in members:
                prev = light.visible_level()
                light.set_level(level, fading_seconds=fading_seconds, fade_origin=wire)
                changes.append((light.address, prev, light.level))
            self.invalidate_groups_sharing(members, except_group=group_num)
            changes.append((wire, event_prev, group.level))
            return changes
        return []

    def apply_per_light_level(
        self,
        wire: int,
        choose: Callable[[Light], int],
        *,
        fading_seconds: int = 0,
    ) -> list[tuple[int, int, int]]:
        """Apply a per-light level choice; return (wire, previous, new) for events."""
        changes: list[tuple[int, int, int]] = []
        if wire == 255:
            for light in self.lights.values():
                prev = light.visible_level()
                light.set_level(
                    choose(light), fading_seconds=fading_seconds, fade_origin=wire
                )
                changes.append((light.address, prev, light.level))
            for group in self.groups.values():
                prev = group.level
                group.last_scene_current = False
                gl = self.sync_group_level(group)
                if gl is not None:
                    changes.append((64 + group.number, prev, gl))
            return changes
        if wire <= 63:
            light = self.lights.get(wire)
            if light is None:
                return []
            prev = light.visible_level()
            light.set_level(
                choose(light), fading_seconds=fading_seconds, fade_origin=wire
            )
            self.invalidate_parent_groups(light)
            changes.append((wire, prev, light.level))
            return changes
        if 64 <= wire <= 79:
            group_num = wire - 64
            group = self.groups.get(group_num)
            if group is None:
                return []
            event_prev = self.event_prev_for_group(group)
            members = self.lights_in_group(group_num)
            for light in members:
                prev = light.visible_level()
                light.set_level(
                    choose(light), fading_seconds=fading_seconds, fade_origin=wire
                )
                changes.append((light.address, prev, light.level))
            group.last_scene_current = False
            self.invalidate_groups_sharing(members, except_group=group_num)
            gl = self.sync_group_level(group)
            if gl is not None:
                changes.append((wire, event_prev, gl))
            return changes
        return []

    def apply_colour(self, wire: int, colour: Colour) -> list[int]:
        """Apply colour; return wire targets that changed (for events)."""
        targets: list[int] = []
        if wire == 255:
            for light in self.lights.values():
                light.set_colour(copy(colour))
                targets.append(light.address)
            for group in self.groups.values():
                group.last_scene_current = False
                targets.append(64 + group.number)
            return targets
        if wire <= 63:
            light = self.lights.get(wire)
            if light is None:
                return []
            light.set_colour(copy(colour))
            self.invalidate_parent_groups(light)
            return [wire]
        if 64 <= wire <= 79:
            group_num = wire - 64
            group = self.groups.get(group_num)
            if group is None:
                return []
            group.last_scene_current = False
            members = self.lights_in_group(group_num)
            for light in members:
                light.set_colour(copy(colour))
                targets.append(light.address)
            self.invalidate_groups_sharing(members, except_group=group_num)
            targets.append(wire)
            return targets
        return []

    def apply_inhibit(self, wire: int, seconds: int) -> list[int]:
        """Store inhibit duration; return affected wire targets."""
        seconds = max(0, min(65535, int(seconds)))
        targets: list[int] = []
        if wire == 255:
            for light in self.lights.values():
                light.set_inhibit(seconds)
                targets.append(light.address)
            for group in self.groups.values():
                group.set_inhibit(seconds)
                targets.append(64 + group.number)
            return targets
        if wire <= 63:
            light = self.lights.get(wire)
            if light is None:
                return []
            light.set_inhibit(seconds)
            return [wire]
        if 64 <= wire <= 79:
            group_num = wire - 64
            group = self.groups.get(group_num)
            if group is None:
                return []
            group.set_inhibit(seconds)
            for light in self.lights_in_group(group_num):
                light.set_inhibit(seconds)
                targets.append(light.address)
            targets.append(wire)
            return targets
        return []

    def clear_fade(self, wire: int) -> list[tuple[int, int, int]]:
        """Stop fades started on this wire; return (target, prev_visible, frozen) for events."""
        changes: list[tuple[int, int, int]] = []

        def _freeze(light: Light, origin: int) -> None:
            if light.fade_origin != origin or light.fading_until is None:
                return
            prev = light.visible_level(expire=False)
            light.clear_fade(freeze=True)
            changes.append((light.address, prev, light.level))

        if wire == 255:
            for light in self.lights.values():
                _freeze(light, 255)
            for group in self.groups.values():
                prev = group.level
                gl = self.sync_group_level(group)
                if gl is not None and gl != prev:
                    changes.append((64 + group.number, prev, gl))
            return changes
        if wire <= 63:
            light = self.lights.get(wire)
            if light is not None:
                before = len(changes)
                _freeze(light, wire)
                if len(changes) > before:
                    self.invalidate_parent_groups(light)
            return changes
        if 64 <= wire <= 79:
            group_num = wire - 64
            group = self.groups.get(group_num)
            stored_prev = group.level if group is not None else 0
            for light in self.lights_in_group(group_num):
                _freeze(light, wire)
            if group is not None and changes:
                gl = self.sync_group_level(group, clear_scene_if_mixed=True)
                if gl is not None:
                    changes.append((wire, stored_prev, gl))
            return changes
        return changes

    def collect_fade_progress(self) -> list[tuple[int, int, int]]:
        """LEVEL_CHANGE_V2 progress/completion tuples: (wire, current, target).

        Mid-fade repeats only when the fade's configured duration exceeds
        FADE_PROGRESS_MIN_MS (real controllers ≈ every 500ms). Completions
        (current == target) are always emitted when a fade expires.
        """
        now = time.time()
        pending: list[tuple[Light, int, int, Optional[int]]] = []
        for light in self.lights.values():
            if (
                light.fading_until is None
                or light.fade_to is None
                or light.fade_started_at is None
            ):
                continue
            total_ms = (light.fading_until - light.fade_started_at) * 1000.0
            origin = light.fade_origin
            if now >= light.fading_until:
                dest = int(light.fade_to)
                pending.append((light, dest, dest, origin))
            elif total_ms > FADE_PROGRESS_MIN_MS:
                pending.append(
                    (light, light.visible_level(expire=False), int(light.fade_to), origin)
                )

        changes: list[tuple[int, int, int]] = []
        involved: dict[int, set[int]] = {}
        for light, current, dest, origin in pending:
            if light.fading_until is not None and now >= light.fading_until:
                light._expire_fade_if_due()
            changes.append((light.address, current, dest))
            # Track group-origin (64–79) and broadcast-origin (255) fades so we
            # can synthesize companion LEVEL_CHANGE_V2 on group wires when agreed.
            if origin is not None and (64 <= origin <= 79 or origin == 255):
                involved.setdefault(origin, set()).add(light.address)

        def _append_agreed_group(group_num: int, addrs: set[int], wire: int) -> None:
            group = self.groups.get(group_num)
            pool = [m for m in self.lights_in_group(group_num) if m.address in addrs]
            if not pool:
                return
            currents = {m.visible_level(expire=False) for m in pool}
            dests = {destination_level(m) for m in pool}
            if len(currents) != 1 or len(dests) != 1:
                return
            current = next(iter(currents))
            dest = next(iter(dests))
            if group is not None and current == dest:
                group.level = dest
            changes.append((wire, current, dest))

        for origin in sorted(o for o in involved if 64 <= o <= 79):
            _append_agreed_group(origin - 64, involved[origin], origin)

        # Broadcast fades: emit each group wire when that group's fading members agree
        if 255 in involved:
            addrs = involved[255]
            for group_num in sorted(self.groups):
                _append_agreed_group(group_num, addrs, 64 + group_num)

        return changes

    def apply_scene(
        self, wire: int, scene: int, *, fading_seconds: float = 0
    ) -> SceneEffects:
        """Apply scene; return SCENE/LEVEL/COLOUR event payloads to emit."""
        effects = SceneEffects()
        origin = wire if fading_seconds > 0 else None

        def _apply_ecg(light: Light) -> None:
            prev = light.visible_level()
            light.apply_scene(scene, fading_seconds=fading_seconds, fade_origin=origin)
            effects.add_ecg(light, scene, prev)

        def _begin_group_scene(group: Group) -> int:
            event_prev = self.event_prev_for_group(group)
            group.last_scene = scene
            group.last_scene_current = True
            return event_prev

        def _group_companions(
            group_num: int, event_prev: int, members: list[Light]
        ) -> None:
            group_wire = 64 + group_num
            effects.scenes.append(group_wire)
            dests = {destination_level(m) for m in members}
            if len(dests) == 1:
                effects.levels.append((group_wire, event_prev, next(iter(dests))))
            if any(scene_colour_bytes(m, scene) is not None for m in members):
                colour = self.agreed_member_colour(group_num)
                if colour is not None:
                    effects.colours.append((group_wire, colour.to_bytes()))

        if wire == 255:
            for light in self.lights.values():
                _apply_ecg(light)
            for group in self.groups.values():
                members = self.lights_in_group(group.number)
                event_prev = _begin_group_scene(group)
                self.sync_group_level(group)
                _group_companions(group.number, event_prev, members)
            return effects

        if wire <= 63:
            light = self.lights.get(wire)
            if light is None:
                return effects
            _apply_ecg(light)
            # Parent groups: sync stored level / clear scene-current, but do not emit
            self.invalidate_parent_groups(light)
            return effects

        if 64 <= wire <= 79:
            group_num = wire - 64
            group = self.groups.get(group_num)
            if group is None:
                return effects
            event_prev = _begin_group_scene(group)
            members = self.lights_in_group(group_num)
            for light in members:
                _apply_ecg(light)
            self.invalidate_groups_sharing(members, except_group=group_num)
            self.sync_group_level(group)
            _group_companions(group_num, event_prev, members)
            return effects

        return effects


def _as_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, str):
        return int(value, 0)
    return int(value)


def _default_last_active(item: dict[str, Any]) -> int:
    """Default last_active_level: current level if on, else 254 (not min_level)."""
    if item.get("last_active_level") is not None:
        return _as_int(item.get("last_active_level"))
    level = _as_int(item.get("level"))
    return level if level > 0 else 254


def _parse_colour(raw: Any) -> Optional[Colour]:
    if not raw:
        return None
    return Colour(
        type=str(raw.get("type", "tc")).lower(),
        kelvin=raw.get("kelvin"),
        r=raw.get("r"),
        g=raw.get("g"),
        b=raw.get("b"),
        w=raw.get("w"),
        a=raw.get("a"),
        f=raw.get("f"),
        x=raw.get("x"),
        y=raw.get("y"),
    )


def _parse_scene_levels(raw: Any) -> list[Optional[int]]:
    levels: list[Optional[int]] = [None] * 12
    if not raw:
        return levels
    for i, value in enumerate(raw[:12]):
        levels[i] = None if value is None else int(value)
    return levels


def _parse_scene_colours(raw: Any) -> list[Optional[Colour]]:
    colours: list[Optional[Colour]] = [None] * 12
    if not raw:
        return colours
    for i, value in enumerate(raw[:12]):
        colours[i] = _parse_colour(value)
    return colours


def load_world(path: str | Path) -> World:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    ctrl = data.get("controller") or {}
    lights: dict[int, Light] = {}
    for item in data.get("lights") or []:
        features = item.get("colour_features") or {}
        limits_raw = item.get("colour_temp_limits")
        limits = ColourTempLimits(**limits_raw) if limits_raw else None
        addr = int(item["address"])
        if not 0 <= addr <= 63:
            raise ValueError(f"Light address must be 0-63, got {addr}")
        if addr in lights:
            raise ValueError(f"Duplicate light address {addr}")
        lights[addr] = Light(
            address=addr,
            label=str(item.get("label", f"Light {addr}")),
            serial=_as_int(item.get("serial")),
            level=_as_int(item.get("level")),
            min_level=_as_int(item.get("min_level"), 1),
            max_level=_as_int(item.get("max_level"), 254),
            last_active_level=_default_last_active(item),
            last_scene=_as_int(item.get("last_scene")),
            last_scene_current=bool(item.get("last_scene_current", False)),
            cg_types=[int(x) for x in (item.get("cg_types") or [])],
            colour=_parse_colour(item.get("colour")),
            colour_features=ColourFeatures(
                supports_xy=bool(features.get("supports_xy", False)),
                supports_tunable=bool(features.get("supports_tunable", False)),
                primary_count=int(features.get("primary_count", 0)),
                rgbwaf_channels=int(features.get("rgbwaf_channels", 0)),
            ),
            colour_temp_limits=limits,
            groups=[int(g) for g in (item.get("groups") or [])],
            scene_levels=_parse_scene_levels(item.get("scene_levels")),
            scene_colours=_parse_scene_colours(item.get("scene_colours")),
            status=_as_int(item.get("status")),
        )
        # Keep last_active aligned when starting on; never leave 0
        lt = lights[addr]
        if lt.level > 0:
            lt.last_active_level = lt.level
        elif lt.last_active_level <= 0:
            lt.last_active_level = 254

    groups: dict[int, Group] = {}
    for item in data.get("groups") or []:
        number = int(item["number"])
        if not 0 <= number <= 15:
            raise ValueError(f"Group number must be 0-15, got {number}")
        if number in groups:
            raise ValueError(f"Duplicate group number {number}")
        scenes_raw = item.get("scenes") or {}
        scenes = {int(k): str(v) for k, v in scenes_raw.items()}
        for scene in scenes:
            if not 0 <= scene <= 11:
                raise ValueError(f"Scene must be 0-11, got {scene}")
        groups[number] = Group(
            number=number,
            label=str(item.get("label", f"Group {number}")),
            level=_as_int(item.get("level")),
            scenes=scenes,
        )

    devices: dict[int, Device] = {}
    for item in data.get("devices") or []:
        addr = int(item["address"])
        if not 0 <= addr <= 63:
            raise ValueError(f"Device address must be 0-63, got {addr}")
        if addr in devices:
            raise ValueError(f"Duplicate device address {addr}")
        instances: list[Instance] = []
        seen_inst: set[int] = set()
        for inst in item.get("instances") or []:
            type_raw = inst.get("type", "push_button")
            if isinstance(type_raw, str):
                type_code = INSTANCE_TYPES.get(type_raw.lower())
            else:
                type_code = INSTANCE_TYPES.get(int(type_raw))
            if type_code is None:
                raise ValueError(f"Unsupported instance type for zencontrol-tpi: {type_raw}")
            number = int(inst["number"])
            if not 0 <= number <= 31:
                raise ValueError(f"Instance number must be 0-31, got {number}")
            if number in seen_inst:
                raise ValueError(f"Duplicate instance {number} on device {addr}")
            seen_inst.add(number)

            timers_raw = inst.get("timers")
            timers: Optional[InstanceTimers] = None
            if timers_raw:
                elapsed = _as_int(timers_raw.get("last_detect"))
                timers = InstanceTimers(
                    deadtime=_as_int(timers_raw.get("deadtime"), 1),
                    hold=_as_int(timers_raw.get("hold"), 60),
                    report=_as_int(timers_raw.get("report"), 20),
                    last_motion_at=time.time() - max(0, elapsed),
                )
            elif int(type_code) == 0x03:
                # Occupancy sensors need timers for zencontrol-python interview()
                timers = InstanceTimers()

            instances.append(
                Instance(
                    number=number,
                    type=int(type_code),
                    label=str(inst.get("label", "")),
                    timers=timers,
                    active=bool(inst.get("active", True)),
                    error=bool(inst.get("error", False)),
                )
            )
        devices[addr] = Device(
            address=addr,
            label=str(item.get("label", f"Device {addr}")),
            serial=_as_int(item.get("serial")),
            instances=instances,
        )

    profiles_section = data.get("profiles") or {}
    profiles: dict[int, Profile] = {}
    for item in profiles_section.get("items") or []:
        number = int(item["number"])
        profiles[number] = Profile(
            number=number,
            label=str(item.get("label", f"Profile {number}")),
        )

    sysvars: dict[int, SystemVariable] = {}
    for item in data.get("system_variables") or []:
        vid = int(item["id"])
        if not 0 <= vid <= 147:
            raise ValueError(f"System variable id must be 0-147, got {vid}")
        simulate = None
        if item.get("simulate") is not None:
            simulate = _as_int(item.get("simulate"))
            if simulate < 0 or simulate > _SYSVAR_VALUE_MAX:
                raise ValueError(
                    f"System variable {vid} simulate must be 0-{_SYSVAR_VALUE_MAX}, got {simulate}"
                )
        value = _as_int(item.get("value"))
        if simulate is not None:
            value = daylight_sine_value(simulate)
        sysvars[vid] = SystemVariable(
            id=vid,
            name=str(item.get("name", f"Var {vid}")),
            value=value,
            simulate=simulate,
        )

    version_raw = ctrl.get("version") or [2, 2, 11]
    version = (int(version_raw[0]), int(version_raw[1]), int(version_raw[2]))

    world = World(
        bind_host=str(ctrl.get("bind_host", "0.0.0.0")),
        bind_port=int(ctrl.get("bind_port", 5108)),
        mac=mac_from_string(str(ctrl.get("mac", "02:00:00:00:00:01"))),
        label=str(ctrl.get("label", "Simulator")),
        version=version,
        startup_complete=bool(ctrl.get("startup_complete", True)),
        dali_ready=bool(ctrl.get("dali_ready", True)),
        lights=lights,
        groups=groups,
        devices=devices,
        profiles=profiles,
        current_profile=int(profiles_section.get("current", 0)),
        last_scheduled_profile=int(profiles_section.get("last_scheduled", 0)),
        system_variables=sysvars,
        event_mode=_as_int(ctrl.get("event_mode"), 0x01),
        heartbeat_interval=float(ctrl.get("heartbeat_interval", 5)),
        heartbeat_ecd=(
            int(ctrl["heartbeat_ecd"]) if ctrl.get("heartbeat_ecd") is not None else None
        ),
        heartbeat_instance=(
            int(ctrl["heartbeat_instance"])
            if ctrl.get("heartbeat_instance") is not None
            else None
        ),
        dim_time_ms=max(0, _as_int(ctrl.get("dim_time_ms"), 0)),
        fitting_number=str(ctrl.get("fitting_number", "1")),
    )
    _validate_world(world)
    return world


def _validate_world(world: World) -> None:
    """Warn about config inconsistencies that break zencontrol-python discovery."""
    log = logging.getLogger(__name__)
    for light in world.lights.values():
        for group in light.groups:
            if group not in world.groups:
                log.warning("Light %s references missing group %s", light.address, group)
        if light.colour_features.supports_tunable and 8 not in light.cg_types:
            log.warning(
                "Light %s has supports_tunable but cg_types lacks 8 "
                "(zencontrol-python will not detect colour temperature)",
                light.address,
            )
        channels = light.colour_features.rgbwaf_channels
        if channels and channels not in (3, 4, 5):
            log.warning(
                "Light %s rgbwaf_channels=%s — zencontrol-python expects 3/4/5 for RGB/RGBW/RGBWW",
                light.address,
                channels,
            )
        if channels and 8 not in light.cg_types:
            log.warning(
                "Light %s has rgbwaf_channels but cg_types lacks 8",
                light.address,
            )
    if world.current_profile and world.current_profile not in world.profiles:
        log.warning("current profile %s is not in profiles list", world.current_profile)

    # Large gaps in system variable IDs cause get_system_variables(give_up_after=N) to stop early
    ids = sorted(world.system_variables)
    for a, b in zip(ids, ids[1:]):
        if b - a > 10:
            log.warning(
                "System variable gap %s→%s > 10 — zencontrol-python may stop scanning early",
                a,
                b,
            )



# --- encoding helpers used by handlers ---

def bitmap_from_addresses(addresses: list[int], max_bits: int = 64) -> bytes:
    nbytes = (max_bits + 7) // 8
    out = bytearray(nbytes)
    for addr in addresses:
        if 0 <= addr < max_bits:
            out[addr // 8] |= 1 << (addr % 8)
    return bytes(out)


def group_membership_bytes(groups: list[int]) -> bytes:
    hi = 0
    lo = 0
    for g in groups:
        if 0 <= g <= 7:
            lo |= 1 << g
        elif 8 <= g <= 15:
            hi |= 1 << (g - 8)
    return bytes([hi, lo])


def scene_bitmask_bytes(scenes: list[int] | dict[int, Any]) -> bytes:
    numbers = list(scenes.keys()) if isinstance(scenes, dict) else list(scenes)
    hi = 0
    lo = 0
    for s in numbers:
        if 0 <= s <= 7:
            lo |= 1 << s
        elif 8 <= s <= 15:
            hi |= 1 << (s - 8)
    return bytes([hi, lo])


def int_to_be(value: int, length: int) -> bytes:
    return int(value).to_bytes(length, "big", signed=False)


def signed_be16(value: int) -> bytes:
    return int(value).to_bytes(2, "big", signed=True)
