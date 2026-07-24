"""Asyncio UDP/TCP server for the Zencontrol TPI Advanced simulator."""

from __future__ import annotations

import asyncio
import logging
import socket

from .events import EventEmitter
from .handlers import CommandDispatcher
from .protocol import (
    MAX_TCP_SESSIONS,
    ParseFailure,
    Request,
    ResponseType,
    build_error,
    extract_request_frame,
    parse_request,
)
from .world import (
    FADE_PROGRESS_INTERVAL_S,
    SYSVAR_SIMULATE_INTERVAL,
    Colour,
    LevelChange,
    World,
    daylight_sine_value,
)

logger = logging.getLogger(__name__)


def dispatch_request(dispatcher: CommandDispatcher, data: bytes) -> bytes | None:
    """Parse one request frame and return a response, or None if unparseable."""
    parsed = parse_request(data)
    if parsed is None:
        return None
    if isinstance(parsed, ParseFailure):
        dispatcher.error_count += 1
        return build_error(parsed.seq, parsed.error)
    assert isinstance(parsed, Request)
    return dispatcher.handle(parsed)


class SimulatorProtocol(asyncio.DatagramProtocol):
    def __init__(self, dispatcher: CommandDispatcher) -> None:
        self.dispatcher = dispatcher
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        sockname = transport.get_extra_info("sockname")
        logger.info("Listening for TPI commands on UDP %s:%s", sockname[0], sockname[1])

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        response = dispatch_request(self.dispatcher, data)
        if response is None:
            logger.debug("Dropping unparseable packet from %s (%d bytes)", addr, len(data))
            return
        if response[0] == ResponseType.ERROR:
            logger.debug("Bad request from %s", addr)
        if self.transport is not None:
            self.transport.sendto(response, addr)

    def error_received(self, exc: Exception) -> None:
        logger.error("UDP error: %s", exc)


class Simulator:
    def __init__(self, world: World) -> None:
        self.world = world
        self.events = EventEmitter(world)
        self.dispatcher = CommandDispatcher(world, self.events)
        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: SimulatorProtocol | None = None
        self._tcp_server: asyncio.Server | None = None
        self._tcp_sessions = 0
        self._stop: asyncio.Event | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._sysvar_sim_task: asyncio.Task[None] | None = None
        self._fade_progress_task: asyncio.Task[None] | None = None

    @property
    def bind_port(self) -> int:
        """Actual bound port (resolves ephemeral 0 after start)."""
        if self._transport is not None:
            return int(self._transport.get_extra_info("sockname")[1])
        return self.world.bind_port

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.world.bind_host, self.world.bind_port))
        self._transport, self._protocol = await loop.create_datagram_endpoint(
            lambda: SimulatorProtocol(self.dispatcher),
            sock=sock,
        )
        # Share the resolved port so TCP and UDP match when bind_port was 0.
        host = self.world.bind_host
        port = self.bind_port
        self.world.bind_port = port

        self._tcp_server = await asyncio.start_server(
            self._handle_tcp_client,
            host=host,
            port=port,
            reuse_address=True,
        )
        logger.info(
            "Listening for TPI commands on TCP %s:%s (max %d sessions)",
            host,
            port,
            MAX_TCP_SESSIONS,
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

        simulated = [v for v in self.world.system_variables.values() if v.simulate is not None]
        if simulated:
            names = ", ".join(f"{v.id}:{v.name}(max={v.simulate})" for v in simulated)
            logger.info(
                "System variable daylight simulate every %.0fs — %s",
                SYSVAR_SIMULATE_INTERVAL,
                names,
            )
            self._sysvar_sim_task = asyncio.create_task(
                self._sysvar_simulate_loop(),
                name="zencontrol-sysvar-simulate",
            )

        self._fade_progress_task = asyncio.create_task(
            self._fade_progress_loop(),
            name="zencontrol-fade-progress",
        )

    async def stop(self) -> None:
        for attr in ("_fade_progress_task", "_sysvar_sim_task", "_heartbeat_task"):
            task = getattr(self, attr)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                setattr(self, attr, None)
        if self._tcp_server is not None:
            self._tcp_server.close()
            await self._tcp_server.wait_closed()
            self._tcp_server = None
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

    async def _handle_tcp_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        if self._tcp_sessions >= MAX_TCP_SESSIONS:
            logger.warning(
                "Rejecting TCP client %s — already at max %d sessions",
                peer,
                MAX_TCP_SESSIONS,
            )
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            return

        self._tcp_sessions += 1
        logger.info("TCP client connected %s (%d/%d)", peer, self._tcp_sessions, MAX_TCP_SESSIONS)
        buf = bytearray()
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                buf.extend(chunk)
                while True:
                    frame = extract_request_frame(buf)
                    if frame is None:
                        break
                    response = dispatch_request(self.dispatcher, frame)
                    if response is None:
                        logger.debug("TCP drop unparseable frame from %s (%d bytes)", peer, len(frame))
                        continue
                    writer.write(response)
                    await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        except Exception:
            logger.exception("TCP session error from %s", peer)
        finally:
            self._tcp_sessions = max(0, self._tcp_sessions - 1)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            logger.info("TCP client disconnected %s (%d/%d)", peer, self._tcp_sessions, MAX_TCP_SESSIONS)

    async def _heartbeat_loop(self) -> None:
        interval = self.world.heartbeat_interval
        while True:
            await asyncio.sleep(interval)
            self.events.occupancy_heartbeat()

    def tick_sysvar_simulation(self, *, seconds_since_midnight: float | None = None) -> list[tuple[int, int]]:
        """Refresh simulated system variables; return list of (id, new_value) that changed."""
        changed: list[tuple[int, int]] = []
        for var in self.world.system_variables.values():
            if var.simulate is None:
                continue
            new_value = daylight_sine_value(
                var.simulate, seconds_since_midnight=seconds_since_midnight
            )
            if new_value == var.value:
                continue
            var.value = new_value
            self.events.system_variable_change(var.id, new_value, magnitude=0)
            changed.append((var.id, new_value))
        return changed

    async def _sysvar_simulate_loop(self) -> None:
        while True:
            await asyncio.sleep(SYSVAR_SIMULATE_INTERVAL)
            changed = self.tick_sysvar_simulation()
            for var_id, value in changed:
                logger.debug("Simulated system variable %s → %s", var_id, value)

    def tick_fade_progress(self) -> list[LevelChange]:
        """Emit LEVEL_CHANGE_V2 progress/completion for active fades; return emitted tuples."""
        changes = self.world.collect_fade_progress()
        self.events.emit_level_changes(changes)
        return changes

    async def _fade_progress_loop(self) -> None:
        while True:
            await asyncio.sleep(FADE_PROGRESS_INTERVAL_S)
            self.tick_fade_progress()

    async def run_forever(self, *, interactive: bool = False) -> None:
        await self.start()
        self._stop = asyncio.Event()
        console_task: asyncio.Task[None] | None = None
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

    def inject_absolute_input(self, ecd: int, instance: int, value: int) -> bool:
        return self.events.absolute_input(ecd, instance, value)

    def inject_level(self, wire: int, level: int) -> None:
        """Mutate + emit LEVEL_CHANGE_V2 as if DALI_ARC_LEVEL was received."""
        self.events.apply_and_emit_level(wire, level)

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

    def inject_colour(self, wire: int, colour: Colour) -> None:
        """Mutate + emit COLOUR_CHANGE as if DALI_COLOUR was received."""
        self.events.apply_and_emit_colour(wire, colour)

    async def _interactive_console(self) -> None:
        """Read stdin commands to inject events while the server runs.

        Commands:
          help
          button <ecd> <instance>
          hold <ecd> <instance>
          occupy <ecd> <instance> [0|1]
          absolute <ecd> <instance> <value>
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
            "(button / hold / occupy / absolute / level / scene / colour / "
            "profile / stats / quit)"
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
        parts = line.split()
        match [parts[0].lower(), *parts[1:]]:
            case ["quit" | "exit" | "q"]:
                if self._stop is not None:
                    self._stop.set()
            case ["help" | "?"]:
                print(
                    "Commands:\n"
                    "  button <ecd> <instance>\n"
                    "  hold <ecd> <instance>\n"
                    "  occupy <ecd> <instance> [0|1]\n"
                    "  absolute <ecd> <instance> <value>  # 0-65535\n"
                    "  level <wire> <0-254>     # ECG 0-63, group 64-79, broadcast 255\n"
                    "  scene <wire> <0-11>\n"
                    "  colour <wire> tc <kelvin>\n"
                    "  colour <wire> rgb <r> <g> <b>\n"
                    "  profile <id>\n"
                    "  stats\n"
                    "  quit"
                )
            case ["stats"]:
                print(
                    f"requests={self.dispatcher.request_count} "
                    f"errors={self.dispatcher.error_count} "
                    f"events={self.events.sent_count} "
                    f"tcp={self._tcp_sessions}/{MAX_TCP_SESSIONS} "
                    f"mode=0x{self.world.event_mode:02x} "
                    f"profile={self.world.current_profile}"
                )
            case ["button", ecd, instance]:
                self.inject_button_press(int(ecd), int(instance))
            case ["hold", ecd, instance]:
                self.inject_button_hold(int(ecd), int(instance))
            case ["occupy" | "occupancy", ecd, instance]:
                self.inject_occupancy(int(ecd), int(instance), occupied=True)
            case ["occupy" | "occupancy", ecd, instance, flag]:
                self.inject_occupancy(int(ecd), int(instance), occupied=bool(int(flag)))
            case ["absolute" | "absinput", ecd, instance, value]:
                self.inject_absolute_input(int(ecd), int(instance), int(value))
            case ["level", wire, level]:
                self.inject_level(int(wire), int(level))
            case ["scene", wire, scene]:
                self.inject_scene(int(wire), int(scene))
            case ["colour", wire, "tc", kelvin]:
                self.inject_colour(int(wire), Colour(type="tc", kelvin=int(kelvin)))
            case ["colour", wire, "rgb", r, g, b]:
                self.inject_colour(
                    int(wire),
                    Colour(type="rgbwaf", r=int(r), g=int(g), b=int(b), w=0, a=0, f=0),
                )
            case ["profile", profile_id]:
                self.inject_profile(int(profile_id))
            case _:
                raise ValueError(f"Unknown command: {line!r} (try 'help')")


def sys_stdin_readline() -> str | None:
    try:
        return input()
    except EOFError:
        return None
