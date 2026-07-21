# Zencontrol TPI Advanced Simulator

UDP simulator of a Zencontrol controller for developing against
[`zencontrol-tpi`](https://github.com/sjwright/zencontrol-tpi) /
[`zencontrol-python`](https://github.com/sjwright/zencontrol-python)
without hardware.

Only implements the TPI Advanced opcodes that **zencontrol-python’s interface
layer** uses (the surface zencontrol-tpi exercises). Unsupported commands may
return `ERROR_UNKNOWN_CMD`.

## Behaviour

- Listens for TPI Advanced commands on **UDP port 5108** (configurable)
- Emits an `IS_OCCUPIED` (0x06) multicast heartbeat every **5 seconds** (configurable
  via `heartbeat_interval`; `0` disables)
- Answers discovery/query commands from a YAML world model
- Control commands **mutate in-memory state** (levels, colour, scenes, profile,
  system variables) and emit matching TPI events on multicast
  `239.255.90.67:6969` (or unicast if the client configures it)
- Queries reflect live in-memory state (not persisted across restarts)
- Group/broadcast control also emits **per-member** level/scene/colour events so
  HA light entities stay in sync without waiting for refresh
- Group scene recall also emits group `COLOUR_CHANGE` when members agree on colour
- ECG level/colour changes clear parent groups’ `last_scene_current`
- `DALI_CUSTOM_FADE` interpolates for `QUERY_LEVEL` while `fade_running` is set;
  `LEVEL_CHANGE_V2` still carries the destination; `DALI_STOP_FADE` freezes at the
  mid-fade level
- `DALI_INHIBIT` stores a timed inhibit flag on targets (no sensor→load automation)
- System variable SET only updates IDs present in YAML (no auto-create)
- Occupancy `last_detect` is computed from a wall-clock last-motion timestamp
  (config `last_detect` means “N seconds ago at load”)
- Empty labels return `NO_ANSWER` (not an empty string) so
  `generic_if_none` clients behave like hardware
- Bad checksum / malformed frames get an `ERROR` reply when a sequence number
  is available
- Unknown DALI targets return `ERROR_UNKNOWN_TARGET`

## Quick start

```bash
cd zencontrol-simulator
./setup-venv.sh                      # creates .venv, installs deps + zencontrol-python
source .venv/bin/activate

zencontrol-simulator                 # uses ./config.yaml
zencontrol-simulator -v              # debug logging
zencontrol-simulator -i              # interactive event injection
```

Point a client at:

| Field | Sample config value |
|-------|---------------------|
| Host  | your machine IP / `127.0.0.1` |
| Port  | `5108` |
| MAC   | `02:00:00:00:00:01` |

### Interactive inject commands (`-i`)

While the simulator is running:

```
button 0 0          # ECD 0, instance 0 press
hold 0 1            # button hold
occupy 0 2          # occupancy / motion pulse (resets last_detect clock)
occupy 0 2 0        # emit unoccupied payload (library still treats as motion)
level 1 128         # ECG/group/broadcast level + events
scene 64 1          # scene recall + events
colour 0 tc 4000    # tunable white
colour 2 rgb 255 0 64
profile 2           # change profile + event
stats
quit
```

Note: `zencontrol-python` treats any `IS_OCCUPIED` event as motion and clears
occupied via the hold timer, so the `occupied=0` inject is mainly for raw
packet testing. Occupancy timer queries return **seconds since last motion**
(wall-clock), matching hardware.

## Sample world

[`config.yaml`](config.yaml) includes:

- Tunable-white light (ECG 0), dimmer (ECG 1), RGB light (ECG 2)
- Two groups with labelled scenes
- Two ECDs: buttons + one occupancy sensor
- Three profiles and two system variables (`Demo Switch`, `Demo Lux Sensor`
  — names include `switch`/`sensor` so zencontrol-tpi exposes them)

Config is validated on load (address ranges, missing group refs, colour
`cg_types` / `rgbwaf_channels` mismatches that would break feature detection).

## Tests

```bash
./setup-venv.sh    # or: pip install -e ".[dev]" && pip install -e ../zencontrol-python
source .venv/bin/activate
pytest
```

Without zencontrol-python installed, unit/state tests still run; live protocol
tests in `tests/test_protocol_live.py` skip via `importorskip`. Those tests
start the simulator on an ephemeral port and drive it through
`zencontrol.ZenProtocol` (unicast TPI Advanced).

## Protocol reference

Zencontrol’s TPI Advanced PDF, version published 20-11-2025.