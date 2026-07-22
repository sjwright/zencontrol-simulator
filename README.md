# Zencontrol TPI Advanced Simulator

Mostly vibe-coded 
UDP/TCP simulator of a Zencontrol controller for developing TPI Advanced
protocol implementations without hardware.

See below for a table of feature completeness. Built in concert with
the human-coded
[`zencontrol-python`](https://github.com/sjwright/zencontrol-python)
protocol implementation and the partially vibe-coded
[`zencontrol-tpi`](https://github.com/sjwright/zencontrol-tpi)
Home Assistant integration.

What follows is mostly AI slop documentation, but the tables are
human-refined and generally quite accurate.

## Behaviour

- Listens for TPI Advanced commands on **UDP and TCP port 5108** (configurable);
  TCP accepts up to **5** concurrent sessions (PDF limit) with stream reassembly
- Emits an `IS_OCCUPIED` (0x06) multicast heartbeat every **5 seconds** by default
(`heartbeat_interval`; `0` disables). Useful for discovery; zencontrol-python
treats each pulse as motion, so occupancy stay held while it runs
- Answers discovery/query commands from a YAML world model
- Control commands **mutate in-memory state** (levels, colour, scenes, profile,
system variables) and emit matching TPI events on multicast
`239.255.90.67:6969` (or unicast if the client configures it)
- Queries reflect live in-memory state (not persisted across restarts)
- Group/broadcast control also emits **per-member** level/scene/colour events so
HA light entities stay in sync without waiting for refresh
- Event targets by address: **ECG** → that ECG only; **group** → members + group
  wire when destinations agree; **broadcast** → every ECG + each group wire when
  that group’s members agree. ECG commands clear parent `last_scene_current` but
  do not emit parent-group events
- Group/broadcast scene recall emits `SCENE_CHANGE`, `LEVEL_CHANGE_V2`, and
  `COLOUR_CHANGE` for members and (when agreed) the group wire
- `controller.dim_time_ms` applies a fade on scene recall; when greater than
  2000ms the simulator re-emits `LEVEL_CHANGE_V2` about every 500ms (current,
  destination) until complete — including agreed group wires for group- and
  broadcast-origin fades
- `DALI_CUSTOM_FADE` interpolates for `QUERY_LEVEL` while `fade_running` is set;
`LEVEL_CHANGE_V2` still carries the destination; long custom fades also get the
~500ms progress ticks; `DALI_STOP_FADE` freezes at the mid-fade level and emits
`LEVEL_CHANGE_V2` with the frozen level
- `DALI_QUERY_MIN_LEVEL` / `MAX_LEVEL` / `FADE_RUNNING` are implemented
- `DALI_INHIBIT` stores a timed inhibit flag on targets (no TPI event; no
  sensor→load automation)
- System variable SET only updates IDs present in YAML (no auto-create)
- System variables with `simulate: <max>` track a daylight sine (0 at midnight,
  max at midday), refreshed every 30 seconds with a `SYSTEM_VARIABLE_CHANGED` event
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


| Field | Sample config value           |
| ----- | ----------------------------- |
| Host  | your machine IP / `127.0.0.1` |
| Port  | `5108`                        |
| MAC   | `02:00:00:00:00:01`           |


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

- Tunable-white lights (ECG 0, 7), dimmers (ECG 1, 4–6), RGB (ECG 2, 8), XY (ECG 3, 9),
  switching gear cg-type 7 (ECG 10–11)
- Hallway dimmers ECG 4–6 with overlapping groups (4+5 in group 2, 5+6 in group 3)
- Colour scenes 0–1 and 8–9 on the living-room tunable-white light
- Groups with labelled scenes (0–3) and without (4–5), matching live dumps
- ECDs covering live shapes: single buttons, 3-button+general sensor, 4-button pads,
  6-button pads, 6-button+general sensor, occupancy+lux combos
- Three profiles and six system variables (switch / lux-sensor / other), with
  `Demo Lux Sensor` using `simulate: 2500` for a daylight sine curve

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

`tests/test_demo_permutations.py` exercises the expanded demo world: hallway
group overlap, switching gear, second TC/RGB/XY fixtures, ECD pad shapes,
and extra system variables.

## Protocol reference

Against Zencontrol’s *Advanced Third Party Interface API Document*
(`Advanced_Third_Party_Interface_API_Document_20_11_2025.pdf`, 20-11-2025).
Unregistered opcodes reply `ERROR_UNKNOWN_CMD` (`0x04`).

### Protocol features

| Spec section                                      | Status     | Notes                                                       |
| ------------------------------------------------- | ---------- | ----------------------------------------------------------- |
| Licenses                                          | No         | No feature gating is simulated                              |
| UDP                                               | Correct    | UDP port `5108`                                             |
| TCP                                               | Correct    | Same port; max 5 sessions; stream frame reassembly          |
| Multicast                                         | Correct    | On `239.255.90.67:6969`                                     |
| Unicast                                           | Correct    | As configured by client                                     |
| RS232                                             | No         | Out of scope                                                |
| RS485                                             | No         | Out of scope                                                |
| Basic Request Frame                               | Correct    | 8-byte basic; wrong length → `INVALID_ARGS`                 |
| Basic Response Frame                              | Correct    | `OK` / `ANSWER` / `NO_ANSWER` / `ERROR` + length + checksum |
| DALI Colour Request Frame                         | Correct    | Opcode `0x0E`; XY / Tc / RGBWAF; arc `0xFF` = colour-only   |
| TPI Dynamic Subframe                              | Correct    | `SET_TPI_EVENT_UNICAST_ADDRESS` (`0x40`)                    |
| DMX Colour Frame                                  | No         | DMX out of scope                                            |
| TPI Event Multicast                               | Correct    | `ZC` header, MAC, target BE16, code, len ≤48, XOR checksum  |
| Sequence Counter                                  | Correct    | Echoed on replies; ERROR when seq available on bad frames   |
| Checksums                                         | Correct    | XOR of preceding bytes (verified vs PDF example)            |
| Error Codes                                       | Correct    | Emits `0x01`, `0x04`, `0xB1`, `0xB8`; other codes unused    |
| DALI Addressing                                   | Correct    | ECG 0–63, groups 64–79, ECD 64–127, broadcast 255           |

### API commands


Status legend: **Simulated** = responds with some degree of correctness / simulation / dynamics; **Partial** = partially simulated, partially stubbed;
**Stub** = static valid response; **N/A** = not implemented (deprecated in spec);
**No** = not implemented.

| Opcode | Command                                | Status     | Extent                                                    |
| ------ | -------------------------------------- | ---------- | --------------------------------------------------------- |
| `0x01` | QUERY_GROUP_LABEL                      | Simulated  | Group 0–15; empty/missing → `NO_ANSWER`                   |
| `0x03` | QUERY_DALI_DEVICE_LABEL                | Simulated  | ECG 0–63 / ECD 64–127; empty → `NO_ANSWER`                |
| `0x04` | QUERY_PROFILE_LABEL                    | Simulated  | Profile id in data mid/lo                                 |
| `0x05` | QUERY_CURRENT_PROFILE_NUMBER           | Simulated  | BE16                                                      |
| `0x07` | QUERY_TPI_EVENT_EMIT_STATE             | Simulated  | Returns `event_mode`                                      |
| `0x08` | ENABLE_TPI_EVENT_EMIT                  | Simulated  | Sets mode bitmask from address byte                       |
| `0x09` | QUERY_GROUP_NUMBERS                    | Simulated  | Sorted group numbers                                      |
| `0x0B` | QUERY_PROFILE_NUMBERS                  | Simulated  | Packed BE16 ids; superseded by `QUERY_PROFILE_INFORMATION` |
| `0x0C` | QUERY_OCCUPANCY_INSTANCE_TIMERS        | Simulated  | deadtime/hold/report + wall-clock last_detect             |
| `0x0D` | QUERY_INSTANCES_BY_ADDRESS             | Partial    | Type/status; state always `0x00`; YAML instance types     |
| `0x0E` | DALI_COLOUR                            | Simulated  | Colour frame; XY/Tc/RGBWAF; optional arc; group/broadcast |
| `0x12` | QUERY_GROUP_BY_NUMBER                  | Partial    | Occupancy always `0x01`; level = max member visible       |
| `0x14` | QUERY_SCENE_NUMBERS_BY_ADDRESS         | Simulated  | Scene indices with configured levels; none → `NO_ANSWER` |
| `0x15` | QUERY_GROUP_MEMBERSHIP_BY_ADDRESS      | Simulated  | Membership bitmap                                         |
| `0x16` | QUERY_DALI_ADDRESSES_WITH_INSTANCES    | Simulated  | ECD wires; paged from start                               |
| `0x1A` | QUERY_SCENE_NUMBERS_FOR_GROUP          | Simulated  | Scene bitmask                                             |
| `0x1B` | QUERY_SCENE_LABEL_FOR_GROUP            | Simulated  | Scenes 0–11                                               |
| `0x1C` | QUERY_CONTROLLER_VERSION_NUMBER        | Simulated  | From YAML `version`                                       |
| `0x1D` | QUERY_CONTROL_GEAR_DALI_ADDRESSES      | Simulated  | ECG presence bitmap                                       |
| `0x1E` | QUERY_SCENE_LEVELS_BY_ADDRESS          | Simulated  | 16 bytes; slots 0–11 live, 12–15 `0xFF`                   |
| `0x21` | QUERY_INSTANCE_GROUPS                  | No         |                                                           |
| `0x22` | QUERY_DALI_FITTING_NUMBER              | Simulated  | `{fitting}.{addr}`; ECD uses `addr+100` (e.g. `1.104`)    |
| `0x23` | QUERY_DALI_INSTANCE_FITTING_NUMBER     | Simulated  | `{fitting}.{ecd+100}.{instance}` (e.g. `1.104.2`)         |
| `0x24` | QUERY_CONTROLLER_LABEL                 | Simulated  | Default config → `"Simulator"`                            |
| `0x25` | QUERY_CONTROLLER_FITTING_NUMBER        | Simulated  | From YAML `controller.fitting_number` (default `"1"`)     |
| `0x26` | QUERY_IS_DALI_READY                    | Stub       | no bus simulation; reads value from YAML flag             |
| `0x27` | QUERY_CONTROLLER_STARTUP_COMPLETE      | Stub       | no boot sequence; reads value from YAML flag              |
| `0x28` | QUERY_OPERATING_MODE_BY_ADDRESS        | Stub       | Always mode `0`; unknown → `0xB8`                         |
| `0x29` | OVERRIDE_DALI_BUTTON_LED_STATE         | Stub       | Always `OK`; no LED model                                 |
| `0x30` | QUERY_LAST_KNOWN_DALI_BUTTON_LED_STATE | Stub       | Always `ANSWER` `[0x01]` (LED off)                        |
| `0x31` | DALI_ADD_TPI_EVENT_FILTER              | Simulated  | Merges mask; reply `OK`                                   |
| `0x32` | QUERY_DALI_TPI_EVENT_FILTERS           | Simulated  | Mode + up to 15 filters; empty → `NO_ANSWER`              |
| `0x33` | DALI_CLEAR_TPI_EVENT_FILTERS           | Simulated  | `OK` if changed else `NO_ANSWER`                          |
| `0x34` | QUERY_DALI_COLOUR                      | Simulated  | Tc / RGBWAF / XY                                          |
| `0x35` | QUERY_DALI_COLOUR_FEATURES             | Simulated  | Feature byte from world                                   |
| `0x36` | SET_SYSTEM_VARIABLE                    | Partial    | Existing YAML IDs only; emits event mag=0                 |
| `0x37` | QUERY_SYSTEM_VARIABLE                  | Simulated  | Signed BE16                                               |
| `0x38` | QUERY_DALI_COLOUR_TEMP_LIMITS          | Simulated  |                                                           |
| `0x40` | SET_TPI_EVENT_UNICAST_ADDRESS          | Simulated  | Dynamic frame; `0.0.0.0`/port 0 clears                    |
| `0x41` | QUERY_TPI_EVENT_UNICAST_ADDRESS        | Simulated  | mode + port + IPv4                                        |
| `0x42` | QUERY_SYSTEM_VARIABLE_NAME             | Simulated  | Empty → `NO_ANSWER`                                       |
| `0x43` | QUERY_PROFILE_INFORMATION              | No         |                                                           |
| `0x44` | QUERY_COLOUR_SCENE_MEMBERSHIP_BY_ADDR  | Simulated  | Scene indices with colour data                            |
| `0x45` | QUERY_COLOUR_SCENE_0_7_DATA_FOR_ADDR   | Simulated  | 8×7-byte blobs; unused = `0xFF`×7                         |
| `0x46` | QUERY_COLOUR_SCENE_8_11_DATA_FOR_ADDR  | Simulated  | 4×7-byte blobs                                            |
| `0xA0` | DALI_INHIBIT                           | Partial    | Timed inhibit flag; no TPI event; no sensor→load automation |
| `0xA1` | DALI_SCENE                             | Simulated  | Scenes 0–11; member level/colour events; broadcast 255    |
| `0xA2` | DALI_ARC_LEVEL                         | Simulated  | Mutates + `LEVEL_CHANGE_V2`; unknown → `0xB8`             |
| `0xA3` | DALI_ON_STEP_UP                        | Simulated  | On-if-off + step                                          |
| `0xA4` | DALI_STEP_DOWN_OFF                     | Simulated  | Off-at-min                                                |
| `0xA5` | DALI_UP                                | Simulated  | No ignite from off                                        |
| `0xA6` | DALI_DOWN                              | Simulated  | Clamps at min                                             |
| `0xA7` | DALI_RECALL_MAX                        | Simulated  | Per-member max                                            |
| `0xA8` | DALI_RECALL_MIN                        | Simulated  | Per-member min                                            |
| `0xA9` | DALI_OFF                               | Simulated  | Level 0                                                   |
| `0xAA` | DALI_QUERY_LEVEL                       | Partial    | ECG/group mid-fade OK; broadcast 255 not supported        |
| `0xAB` | DALI_QUERY_CONTROL_GEAR_STATUS         | Simulated  | ECG / group / broadcast 255 OR                            |
| `0xAC` | DALI_QUERY_CG_TYPE                     | Simulated  | 32-bit LE type mask                                       |
| `0xAD` | DALI_QUERY_LAST_SCENE                  | Simulated  | ECG or group                                              |
| `0xAE` | DALI_QUERY_LAST_SCENE_IS_CURRENT       | Simulated  | Cleared by level/colour on members                        |
| `0xAF` | DALI_QUERY_MIN_LEVEL                   | Simulated  |                                                           |
| `0xB0` | DALI_QUERY_MAX_LEVEL                   | Simulated  |                                                           |
| `0xB1` | DALI_QUERY_FADE_RUNNING                | Simulated  | Status bit `0x10`                                         |
| `0xB2` | DALI_ENABLE_DAPC_SEQ                   | Stub       | Always `NO_ANSWER`; no 250 ms DAPC override               |
| `0xB4` | DALI_CUSTOM_FADE                       | Simulated  | Fade seconds BE16; query interpolates; V2 = destination   |
| `0xB5` | DALI_GO_TO_LAST_ACTIVE_LEVEL           | Simulated  | Per-member; fallback 254                                  |
| `0xB7` | QUERY_DALI_INSTANCE_LABEL              | Simulated  | ECD wire + instance in data lo                            |
| `0xB8` | QUERY_DALI_EAN                         | Stub       | Synthetic GTIN `10000000000 + addr` (6-byte BE)           |
| `0xB9` | QUERY_DALI_SERIAL                      | Simulated  | 8-byte BE serial ECG/ECD                                  |
| `0xC0` | CHANGE_PROFILE_NUMBER                  | Simulated  | `0xFFFF` → last scheduled; emits profile event            |
| `0xC1` | DALI_STOP_FADE                         | Simulated  | Freezes mid-fade + `LEVEL_CHANGE_V2`                      |
| `0x02` | QUERY_SCENE_LABEL                      | N/A        | Legacy; use `QUERY_SCENE_LABEL_FOR_GROUP`                 |
| `0x06` | TRIGGER_SDDP_IDENTIFY                  | No         | Control4 / SDDP out of scope                              |
| `0x0A` | QUERY_SCENE_NUMBERS                    | N/A        | Legacy; use `QUERY_SCENE_NUMBERS_FOR_GROUP`               |
| `0x10` | DMX_COLOUR                             | No         | DMX out of scope                                          |
| `0x13` | QUERY_SCENE_BY_NUMBER                  | N/A        | Legacy; no group context                                  |
| `0x17` | QUERY_DMX_DEVICE_NUMBERS               | No         | DMX out of scope                                          |
| `0x18` | QUERY_DMX_DEVICE_BY_NUMBER             | No         | DMX out of scope                                          |
| `0x19` | QUERY_DMX_LEVEL_BY_CHANNEL             | No         | DMX out of scope                                          |
| `0x20` | QUERY_DMX_DEVICE_LABEL_BY_NUMBER       | No         | DMX out of scope                                          |
| `0xB3` | VIRTUAL_INSTANCE                       | No         | Virtual instances are out of scope                        |
| `0xB6` | QUERY_VIRTUAL_INSTANCES                | No         | Virtual instances are out of scope                        |

Deliberate deviations: broadcast wire **255** only (not **127**); level events
are `**LEVEL_CHANGE_V2` only**; scene level queries return 16 DALI slots with
12–15 as `0xFF`; colour-scene unused slots are `0xFF` × 7.

#### Events

| Code   | Event                         | Status     | Extent                                |
| ------ | ----------------------------- | ---------- | ------------------------------------- |
| `0x00` | BUTTON_PRESS_EVENT            | Simulated  | Inject via `-i`; ECD wire `64+ecd`    |
| `0x01` | BUTTON_HOLD_EVENT             | Simulated  | Inject via `-i`                       |
| `0x02` | ABSOLUTE_INPUT_EVENT          | No         |                                       |
| `0x03` | LEVEL_CHANGE_EVENT            | N/A        | Legacy; use `LEVEL_CHANGE_EVENT_V2`   |
| `0x04` | GROUP_LEVEL_CHANGE_EVENT      | N/A        | Legacy; use V2 on group wire 64–79    |
| `0x05` | SCENE_CHANGE_EVENT            | Simulated  | Emitted on `DALI_SCENE` / inject      |
| `0x06` | OCCUPANCY_EVENT               | Partial    | Inject + optional heartbeat; no load  |
| `0x07` | SYSTEM_VARIABLE_CHANGED_EVENT | Simulated  | On `SET_SYSTEM_VARIABLE` / simulate   |
| `0x08` | COLOUR_CHANGED_EVENT          | Simulated  | On colour / colour scenes             |
| `0x09` | PROFILE_CHANGED_EVENT         | Simulated  | On profile change / inject            |
| `0x0A` | GROUP_OCCUPANCY_EVENT         | No         |                                       |
| `0x0B` | LEVEL_CHANGE_EVENT_V2         | Simulated  | Sole level-change event used          |
