"""TPI event emitter (multicast / unicast) plus synthetic inject helpers."""

from __future__ import annotations

import logging
import socket
from typing import Optional

from .protocol import EventCode, MULTICAST_GROUP, MULTICAST_PORT, build_event
from .world import World

logger = logging.getLogger(__name__)


class EventEmitter:
    def __init__(self, world: World) -> None:
        self.world = world
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        self.sent_count = 0

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def events_enabled(self) -> bool:
        return bool(self.world.event_mode & 0x01)

    def multicast_enabled(self) -> bool:
        return (self.world.event_mode & 0x80) == 0

    def unicast_enabled(self) -> bool:
        return bool(self.world.event_mode & 0x40)

    def filtering_enabled(self) -> bool:
        return bool(self.world.event_mode & 0x02)

    def _filtered(self, target: int, event_code: int, instance: Optional[int] = None) -> bool:
        if not self.filtering_enabled():
            return False
        wire = target & 0xFFFF
        for filt in self.world.event_filters:
            # Filter address is ECG 0-63 / ECD|group wire 64-127 / 0xFF = all
            if filt.address not in (0xFF, wire, wire & 0xFF):
                continue
            # Gear events (no instance) only match instance-wildcard filters;
            # otherwise ECD instance mutes would also mute group wires 64-79.
            if instance is None:
                if filt.instance != 0xFF:
                    continue
            elif filt.instance not in (instance, 0xFF):
                continue
            if filt.mask & (1 << event_code):
                return True
        return False

    def emit(
        self,
        target: int,
        event_code: int | EventCode,
        payload: bytes = b"",
        instance: Optional[int] = None,
    ) -> bool:
        if not self.events_enabled():
            logger.debug("Skip event (emit disabled) code=%s target=%s", event_code, target)
            return False
        code = int(event_code)
        if self._filtered(target, code, instance):
            logger.debug("Filtered event %s target=%s", code, target)
            return False

        packet = build_event(self.world.mac, target, code, payload)
        sent = False
        name = EventCode(code).name if code in EventCode._value2member_map_ else hex(code)

        if self.unicast_enabled() and self.world.unicast_ip and self.world.unicast_port:
            try:
                self._sock.sendto(packet, (self.world.unicast_ip, self.world.unicast_port))
                sent = True
                logger.info(
                    "UNICAST %s target=%s payload=%s -> %s:%s",
                    name,
                    target,
                    payload.hex() or "-",
                    self.world.unicast_ip,
                    self.world.unicast_port,
                )
            except OSError as exc:
                logger.warning("Unicast send failed: %s", exc)

        if self.multicast_enabled():
            try:
                self._sock.sendto(packet, (MULTICAST_GROUP, MULTICAST_PORT))
                sent = True
                logger.info("MULTICAST %s target=%s payload=%s", name, target, payload.hex() or "-")
            except OSError as exc:
                logger.warning("Multicast send failed: %s", exc)

        if sent:
            self.sent_count += 1
        else:
            logger.debug("Event not sent (no destination) %s", name)
        return sent

    def level_change(self, wire_target: int, current: int, target_level: int) -> None:
        self.emit(
            wire_target,
            EventCode.LEVEL_CHANGE_V2,
            bytes([current & 0xFF, target_level & 0xFF]),
        )

    def scene_change(self, wire_target: int, scene: int, active: bool = True) -> None:
        self.emit(
            wire_target,
            EventCode.SCENE_CHANGE,
            bytes([scene & 0xFF, 1 if active else 0]),
        )

    def apply_and_emit_scene(self, wire: int, scene: int) -> list[int]:
        """Mutate world for a scene recall and emit companion TPI events.

        Real controllers emit for the target and each member:
        SCENE_CHANGE, LEVEL_CHANGE_V2, and COLOUR_CHANGE (when colour data applies).
        Long fades (dim_time_ms > 2000) get LEVEL_CHANGE_V2 progress via the
        simulator fade ticker.
        """
        prev_levels: dict[int, int] = {}
        fading = max(0.0, self.world.dim_time_ms / 1000.0)
        if wire == 255:
            prev_levels = {a: lt.visible_level() for a, lt in self.world.lights.items()}
            for group in self.world.groups.values():
                gl = self.world.group_level(group.number)
                if gl is None or gl == 255:
                    prev_levels[64 + group.number] = group.level
                else:
                    prev_levels[64 + group.number] = gl
        elif wire <= 63:
            light = self.world.light(wire)
            if light:
                prev_levels[wire] = light.visible_level()
        elif 64 <= wire <= 79:
            group = self.world.group(wire - 64)
            gl = self.world.group_level(wire - 64)
            if gl is None:
                prev_levels[wire] = group.level if group else 0
            elif gl == 255 and group is not None:
                # Mixed is not a valid event current — use last stored group level
                prev_levels[wire] = group.level
            else:
                prev_levels[wire] = gl
            for light in self.world.lights_in_group(wire - 64):
                prev_levels[light.address] = light.visible_level()

        targets = self.world.apply_scene(wire, scene, fading_seconds=fading)
        for target in targets:
            self.scene_change(target, scene, active=True)
            if target <= 63:
                light = self.world.light(target)
                if light is not None:
                    dest = light.fade_to if light.fade_to is not None else light.level
                    prev = prev_levels.get(target, dest)
                    self.level_change(target, prev, dest)
                    # COLOUR_CHANGE per member when this scene defines colour data
                    if (
                        0 <= scene < len(light.scene_colours)
                        and light.scene_colours[scene] is not None
                        and light.colour is not None
                    ):
                        self.colour_change(target, light.colour.to_bytes())
            elif 64 <= target <= 79:
                members = self.world.lights_in_group(target - 64)
                dests = {
                    (m.fade_to if m.fade_to is not None else m.level) for m in members
                }
                # Group LEVEL_CHANGE_V2 only when members share a destination arc
                if len(dests) == 1:
                    dest = next(iter(dests))
                    prev = prev_levels.get(target, dest)
                    self.level_change(target, prev, dest)
                # COLOUR_CHANGE for the group when members agree after the scene
                scene_has_colour = any(
                    0 <= scene < len(m.scene_colours) and m.scene_colours[scene] is not None
                    for m in members
                )
                if scene_has_colour:
                    colour = self.world.agreed_member_colour(target - 64)
                    if colour is not None:
                        self.colour_change(target, colour.to_bytes())
        return targets

    def colour_change(self, wire_target: int, colour_bytes: bytes) -> None:
        self.emit(wire_target, EventCode.COLOUR_CHANGE, colour_bytes)

    def profile_change(self, profile: int) -> None:
        self.emit(0, EventCode.PROFILE_CHANGE, int(profile).to_bytes(2, "big"))

    def system_variable_change(self, variable: int, value: int, magnitude: int = 0) -> None:
        raw = int(value).to_bytes(4, "big", signed=True)
        mag = int(magnitude).to_bytes(1, "big", signed=True)
        self.emit(variable, EventCode.SYSTEM_VARIABLE_CHANGE, raw + mag)

    # --- Synthetic injectors (for testing clients without DALI hardware) ---

    def button_press(self, ecd: int, instance: int) -> bool:
        self._require_instance(ecd, instance, expect_button=True)
        return self.emit(64 + ecd, EventCode.BUTTON_PRESS, bytes([instance & 0xFF]), instance=instance)

    def button_hold(self, ecd: int, instance: int) -> bool:
        self._require_instance(ecd, instance, expect_button=True)
        return self.emit(64 + ecd, EventCode.BUTTON_HOLD, bytes([instance & 0xFF]), instance=instance)

    def occupancy(self, ecd: int, instance: int, occupied: bool = True) -> bool:
        self._require_instance(ecd, instance, expect_occupancy=True)
        inst = self.world.instance(ecd, instance)
        # zencontrol-python treats any IS_OCCUPIED as motion and starts the hold timer;
        # update last_motion_at so QUERY_OCCUPANCY_INSTANCE_TIMERS advances correctly.
        if inst is not None and inst.timers is not None and occupied:
            inst.timers.note_motion()
        return self.emit(
            64 + ecd,
            EventCode.IS_OCCUPIED,
            bytes([instance & 0xFF, 0x01 if occupied else 0x00]),
            instance=instance,
        )

    def occupancy_heartbeat(self) -> bool:
        """Emit IS_OCCUPIED (0x06) as a keepalive without updating motion timers."""
        target = self.world.heartbeat_target()
        if target is None:
            logger.debug("Heartbeat skipped — no occupancy sensor in world")
            return False
        ecd, instance = target
        return self.emit(
            64 + ecd,
            EventCode.IS_OCCUPIED,
            bytes([instance & 0xFF, 0x01]),
            instance=instance,
        )

    def _require_instance(
        self,
        ecd: int,
        instance: int,
        *,
        expect_button: bool = False,
        expect_occupancy: bool = False,
    ) -> None:
        if not 0 <= ecd <= 63:
            raise ValueError(f"ECD address must be 0-63, got {ecd}")
        device = self.world.device(ecd)
        if device is None:
            raise ValueError(f"No device at ECD {ecd}")
        inst = self.world.instance(ecd, instance)
        if inst is None:
            raise ValueError(f"No instance {instance} on ECD {ecd}")
        if expect_button and inst.type != 0x01:
            raise ValueError(f"Instance {ecd}.{instance} is not a push button")
        if expect_occupancy and inst.type != 0x03:
            raise ValueError(f"Instance {ecd}.{instance} is not an occupancy sensor")
