"""CLI entrypoint: python -m zencontrol_simulator"""

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

from .server import Simulator
from .world import load_world


def default_config_path() -> Path:
    """Prefer cwd config.yaml, else the packaged sample."""
    cwd = Path.cwd() / "config.yaml"
    if cwd.is_file():
        return cwd
    packaged = Path(__file__).resolve().parent / "config.yaml"
    if packaged.is_file():
        return packaged
    # Editable checkout: repo-root sample next to the package
    return Path(__file__).resolve().parents[1] / "config.yaml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Zencontrol TPI Advanced controller simulator",
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=None,
        help="Path to YAML config (default: ./config.yaml, else package sample)",
    )
    parser.add_argument(
        "--host",
        help="Override bind host from config",
    )
    parser.add_argument(
        "--port",
        type=int,
        help="Override bind port from config (default 5108)",
    )
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="Read stdin commands to inject button/occupancy/level/scene/colour events",
    )
    parser.add_argument(
        "-s",
        "--startup-delay",
        type=float,
        metavar="N",
        default=0,
        help=(
            "Seconds before QUERY_CONTROLLER_STARTUP_COMPLETE reports complete "
            "(incomplete / NO_ANSWER until N seconds after process start)"
        ),
    )
    parser.add_argument(
        "-t",
        "--simulate-timing",
        action="store_true",
        help=(
            "Delay replies to approximate live controller RTT "
            "(10ms default; 20/30/50/100ms for select slow queries)"
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Debug logging",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config_path = args.config or default_config_path()
    if not config_path.is_file():
        logging.error("Config not found: %s", config_path)
        sys.exit(1)

    try:
        world = load_world(config_path)
    except Exception as exc:
        logging.error("Failed to load config: %s", exc)
        sys.exit(1)

    if args.host:
        world.bind_host = args.host
    if args.port:
        world.bind_port = args.port
    if args.startup_delay and args.startup_delay > 0:
        world.startup_delay_s = float(args.startup_delay)
        world.startup_complete = True  # eventual state after the delay
        world.started_at = time.time()
    if args.simulate_timing:
        world.simulate_response_latency = True

    simulator = Simulator(world)
    try:
        asyncio.run(simulator.run_forever(interactive=args.interactive))
    except (KeyboardInterrupt, SystemExit):
        logging.info("Shutting down")


if __name__ == "__main__":
    main()
