"""Asyncio UDP server for the Zencontrol TPI Advanced simulator."""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Optional

from .events import EventEmitter
from .handlers import CommandDispatcher
from .protocol import ParseFailure, Request, build_error, parse_request
from .world import World

logger = logging.getLogger(__name__)


class SimulatorProtocol(asyncio.DatagramProtocol):
    def __init__(self, dispatcher: CommandDispatcher) -> None:
        self.dispatcher = dispatcher
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        sockname = transport.get_extra_info("sockname")
        logger.info("Listening for TPI commands on UDP %s:%s", sockname[0], sockname[1])

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        parsed = parse_request(data)
        if parsed is None:
            logger.debug("Dropping unparseable packet from %s (%d bytes)", addr, len(data))
            return
        if isinstance(parsed, ParseFailure):
            logger.debug("Bad request from %s: %s", addr, parsed.reason)
            if self.transport is not None:
                self.transport.sendto(build_error(parsed.seq, parsed.error), addr)
            self.dispatcher.error_count += 1
            return
        assert isinstance(parsed, Request)
        response = self.dispatcher.handle(parsed)
        if self.transport is not None:
            self.transport.sendto(response, addr)

    def error_received(self, exc: Exception) -> None:
        logger.error("UDP error: %s", exc)


class Simulator:
    def __init__(self, world: World) -> None:
        self.world = world
        self.events = EventEmitter(world)
        self.dispatcher = CommandDispatcher(world, self.events)
        self._transport: Optional[asyncio.DatagramTransport] = None
        self._protocol: Optional[SimulatorProtocol] = None
        self._stop: Optional[asyncio.Event] = None
        self._heartbeat_task: Optional[asyncio.Task[None]] = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.world.bind_host, self.world.bind_port))
        self._transport, self._protocol = await loop.create_datagram_endpoint(
            lambda: SimulatorProtocol(self.dispatcher),
            sock=sock,
        )
        mac = ":".join(f"{b:02x}" for b in self.world.mac)
        logger.info(
            "Zencontrol simulator ready — MAC %s, %d lights, %d groups, %d devices",
            mac,
            len(self.world.lights),
            len(self.world.groups),
            len(self.world.devices),
        )
        if self.world.heartbeat_interval > 0:
            target = self.world.heartbeat_target()
            if target is None:
                logger.warning(
                    "Heartbeat interval %.1fs set but no occupancy sensor configured",
                    self.world.heartbeat_interval,
                )
            else:
                logger.warning(
                    "Heartbeat: IS_OCCUPIED ECD %s.%s every %.1fs "
                    "(discovery keepalive; zencontrol-python occupancy hold "
                    "will not clear while this runs — set heartbeat_interval: 0 to disable)",
                    target[0],
                    target[1],
                    self.world.heartbeat_interval,
                )
                self._heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(),
                    name="zencontrol-heartbeat",
                )

    async def stop(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        self.events.close()
        logger.info(
            "Stopped — %d requests (%d errors), %d events sent",
            self.dispatcher.request_count,
            self.dispatcher.error_count,
            self.events.sent_count,
        )

    async def _heartbeat_loop(self) -> None:
        interval = self.world.heartbeat_interval
        while True:
            await asyncio.sleep(interval)
            self.events.occupancy_heartbeat()

    async def run_forever(self, *, interactive: bool = False) -> None:
        await self.start()
        self._stop = asyncio.Event()
        console_task: Optional[asyncio.Task[None]] = None
        try:
            if interactive:
                console_task = asyncio.create_task(self._interactive_console())
            await self._stop.wait()
        finally:
            if console_task is not None:
                console_task.cancel()
                try:
                    await console_task
                except asyncio.CancelledError:
                    pass
            await self.stop()

    # --- Synthetic injectors (delegate to EventEmitter) ---

    def inject_button_press(self, ecd: int, instance: int) -> bool:
        return self.events.button_press(ecd, instance)

    def inject_button_hold(self, ecd: int, instance: int) -> bool:
        return self.events.button_hold(ecd, instance)

    def inject_occupancy(self, ecd: int, instance: int, occupied: bool = True) -> bool:
        return self.events.occupancy(ecd, instance, occupied=occupied)

    def inject_level(self, wire: int, level: int) -> None:
        """Mutate + emit LEVEL_CHANGE_V2 as if DALI_ARC_LEVEL was received."""
        for target, previous, new in self.world.apply_level(wire, level):
            self.events.level_change(target, previous, new)

    def inject_scene(self, wire: int, scene: int) -> None:
        """Mutate + emit scene/level/colour events as if DALI_SCENE was received."""
        self.events.apply_and_emit_scene(wire, scene)

    def inject_profile(self, profile_id: int) -> None:
        if profile_id not in self.world.profiles and profile_id != 0xFFFF:
            raise ValueError(f"Unknown profile {profile_id}")
        if profile_id == 0xFFFF:
            profile_id = self.world.last_scheduled_profile
        self.world.current_profile = profile_id
        self.events.profile_change(profile_id)

    def inject_colour(self, wire: int, colour) -> None:
        """Mutate + emit COLOUR_CHANGE as if DALI_COLOUR was received."""
        from .world import Colour

        if not isinstance(colour, Colour):
            raise TypeError("colour must be a Colour instance")
        for target in self.world.apply_colour(wire, colour):
            self.events.colour_change(target, colour.to_bytes())

    async def _interactive_console(self) -> None:
        """Read stdin commands to inject events while the server runs.

        Commands:
          help
          button <ecd> <instance>
          hold <ecd> <instance>
          occupy <ecd> <instance> [0|1]
          level <wire> <0-254>
          scene <wire> <0-11>
          colour <wire> tc <kelvin>
          colour <wire> rgb <r> <g> <b>
          profile <id>
          stats
          quit
        """
        loop = asyncio.get_running_loop()
        logger.info(
            "Interactive mode — type 'help' for inject commands "
            "(button / hold / occupy / level / scene / colour / profile / stats / quit)"
        )
        while True:
            if self._stop is not None and self._stop.is_set():
                return
            try:
                line = await loop.run_in_executor(None, sys_stdin_readline)
            except (EOFError, asyncio.CancelledError):
                return
            if line is None:
                if self._stop is not None:
                    self._stop.set()
                return
            line = line.strip()
            if not line:
                continue
            try:
                self._handle_console_line(line)
            except Exception as exc:
                logger.error("%s", exc)
            if self._stop is not None and self._stop.is_set():
                return

    def _handle_console_line(self, line: str) -> None:
        from .world import Colour

        parts = line.split()
        cmd = parts[0].lower()
        if cmd in ("quit", "exit", "q"):
            if hasattr(self, "_stop") and self._stop is not None:
                self._stop.set()
            return
        if cmd in ("help", "?"):
            print(
                "Commands:\n"
                "  button <ecd> <instance>\n"
                "  hold <ecd> <instance>\n"
                "  occupy <ecd> <instance> [0|1]\n"
                "  level <wire> <0-254>     # ECG 0-63, group 64-79, broadcast 255\n"
                "  scene <wire> <0-11>\n"
                "  colour <wire> tc <kelvin>\n"
                "  colour <wire> rgb <r> <g> <b>\n"
                "  profile <id>\n"
                "  stats\n"
                "  quit"
            )
            return
        if cmd == "stats":
            print(
                f"requests={self.dispatcher.request_count} "
                f"errors={self.dispatcher.error_count} "
                f"events={self.events.sent_count} "
                f"mode=0x{self.world.event_mode:02x} "
                f"profile={self.world.current_profile}"
            )
            return
        if cmd == "button" and len(parts) == 3:
            self.inject_button_press(int(parts[1]), int(parts[2]))
            return
        if cmd == "hold" and len(parts) == 3:
            self.inject_button_hold(int(parts[1]), int(parts[2]))
            return
        if cmd in ("occupy", "occupancy") and len(parts) in (3, 4):
            occupied = True if len(parts) == 3 else bool(int(parts[3]))
            self.inject_occupancy(int(parts[1]), int(parts[2]), occupied=occupied)
            return
        if cmd == "level" and len(parts) == 3:
            self.inject_level(int(parts[1]), int(parts[2]))
            return
        if cmd == "scene" and len(parts) == 3:
            self.inject_scene(int(parts[1]), int(parts[2]))
            return
        if cmd == "colour" and len(parts) >= 4:
            wire = int(parts[1])
            kind = parts[2].lower()
            if kind == "tc" and len(parts) == 4:
                self.inject_colour(wire, Colour(type="tc", kelvin=int(parts[3])))
                return
            if kind == "rgb" and len(parts) == 6:
                self.inject_colour(
                    wire,
                    Colour(
                        type="rgbwaf",
                        r=int(parts[3]),
                        g=int(parts[4]),
                        b=int(parts[5]),
                        w=0,
                        a=0,
                        f=0,
                    ),
                )
                return
        if cmd == "profile" and len(parts) == 2:
            self.inject_profile(int(parts[1]))
            return
        raise ValueError(f"Unknown command: {line!r} (try 'help')")


def sys_stdin_readline() -> Optional[str]:
    try:
        return input()
    except EOFError:
        return None
