# Zencontrol TPI Advanced Simulator

UDP/TCP simulator of a Zencontrol controller for developing against
[`zencontrol-tpi`](https://github.com/sjwright/zencontrol-tpi) /
[`zencontrol-python`](https://github.com/sjwright/zencontrol-python)
without hardware.

Only implements the TPI Advanced opcodes that **zencontrol-python’s interface
layer** uses (the surface zencontrol-tpi exercises). Unsupported commands may
return `ERROR_UNKNOWN_CMD`.

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
- Group scene recall also emits group `COLOUR_CHANGE` when members agree on colour
- ECG level/colour changes clear parent groups’ `last_scene_current`
- `DALI_CUSTOM_FADE` interpolates for `QUERY_LEVEL` while `fade_running` is set;
`LEVEL_CHANGE_V2` still carries the destination; `DALI_STOP_FADE` freezes at the
mid-fade level and emits `LEVEL_CHANGE_V2` with the frozen level
- `DALI_QUERY_MIN_LEVEL` / `MAX_LEVEL` / `FADE_RUNNING` are implemented
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

`[config.yaml](config.yaml)` includes:

- Tunable-white light (ECG 0), dimmer (ECG 1), RGB light (ECG 2), XY spotlight (ECG 3)
- Colour scenes 0–1 and 8–9 on the tunable-white light
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

Against Zencontrol’s *Advanced Third Party Interface API Document*
(`Advanced_Third_Party_Interface_API_Document_20_11_2025.pdf`, 20-11-2025).
Unregistered opcodes reply `ERROR_UNKNOWN_CMD` (`0x04`).

### Protocol features


| Spec section                                      | Status          | Notes                                                       |
| ------------------------------------------------- | --------------- | ----------------------------------------------------------- |
| Supported Devices                                 |                 | Mimics one *zencontrol Controller Pro* unit                 |
| Licenses                                          |                 | No feature gating                                           |
| UDP                                               | Correct         | UDP port **5108**                                           |
| TCP                                               | Correct         | Same port; max **5** sessions; stream frame reassembly      |
| Multicast                                         | Correct         | On **239.255.90.67:6969**                                   |
| Unicast                                           | Correct         | As configured by client                                     |
| RS232                                             | Not implemented | Out of scope                                                |
| RS485                                             | Not implemented | Out of scope                                                |
| Basic Request Frame                               | Correct         | 8-byte basic; wrong length → `INVALID_ARGS`                 |
| Basic Response Frame                              | Correct         | `OK` / `ANSWER` / `NO_ANSWER` / `ERROR` + length + checksum |
| DALI Colour Request Frame                         | Correct         | Opcode `0x0E`; XY / Tc / RGBWAF; arc `0xFF` = colour-only   |
| TPI Dynamic Subframe                              | Correct         | `SET_TPI_EVENT_UNICAST_ADDRESS` (`0x40`)                    |
| DMX Colour Frame                                  | Not implemented | `DMX_COLOUR` → `ERROR_UNKNOWN_CMD`                          |
| TPI Event Multicast                               | Correct         | `ZC` header, MAC, target BE16, code, len ≤48, XOR checksum  |
| Sequence Counter                                  | Correct         | Echoed on replies; ERROR when seq available on bad frames   |
| Checksums                                         | Correct         | XOR of preceding bytes (verified vs PDF example)            |
| Error Codes                                       | Partial         | Emits `0x01`, `0x04`, `0xB1`, `0xB8`; other codes unused    |
| DALI Addressing                                   | Complete        | ECG 0–63, groups 64–79, ECD 64–127, broadcast 255           |


### Documented examples


| Spec section                                       | Status          | Notes                                                |
| -------------------------------------------------- | --------------- | ---------------------------------------------------- |
| Examples — QUERY_* / DALI_* / DMX_* (command docs) | Partial         | Covered per-opcode in API table                      |
| Examples — BUTTON_PRESS_EVENT / BUTTON_HOLD_EVENT  | Partial         | Inject via `-i`; framing correct; no DALI automation |
| Examples — ABSOLUTE_INPUT_EVENT                    | Not implemented |                                                      |
| Examples — LEVEL_CHANGE_EVENT                      | Not implemented | Deliberate; V2 only                                  |
| Examples — GROUP_LEVEL_CHANGE_EVENT                | Not implemented | Deliberate; V2 on group wire                         |
| Examples — SCENE_CHANGE_EVENT                      | Correct         | On scene recall (+ member companions)                |
| Examples — OCCUPANCY_EVENT                         | Partial         | Inject + optional heartbeat; no sensor→load          |
| Examples — SYSTEM_VARIABLE_CHANGED_EVENT           | Correct         | On SET; magnitude always 0                           |
| Examples — COLOUR_CHANGED_EVENT                    | Correct         | Tc / RGBWAF / XY; group when members agree           |
| Examples — PROFILE_CHANGED_EVENT                   | Correct         | Target 0, profile BE16                               |
| Examples — GROUP_OCCUPANCY_EVENT                   | Not implemented |                                                      |
| Examples — LEVEL_CHANGE_EVENT_V2                   | Correct         | Sole level-change event used                         |


### API commands


| Opcode | Command                                | Status          | Extent                                                    |
| ------ | -------------------------------------- | --------------- | --------------------------------------------------------- |
| `0x01` | QUERY_GROUP_LABEL                      | Fully simulated | Group 0–15; empty/missing → `NO_ANSWER`                   |
| `0x02` | QUERY_SCENE_LABEL                      | Not implemented | Prefer `QUERY_SCENE_LABEL_FOR_GROUP`                      |
| `0x03` | QUERY_DALI_DEVICE_LABEL                | Fully simulated | ECG 0–63 / ECD 64–127; empty → `NO_ANSWER`                |
| `0x04` | QUERY_PROFILE_LABEL                    | Fully simulated | Profile id in data mid/lo                                 |
| `0x05` | QUERY_CURRENT_PROFILE_NUMBER           | Fully simulated | BE16                                                      |
| `0x06` | TRIGGER_SDDP_IDENTIFY                  | Not implemented | Control4 / SDDP out of scope                              |
| `0x07` | QUERY_TPI_EVENT_EMIT_STATE             | Fully simulated | Returns `event_mode`                                      |
| `0x08` | ENABLE_TPI_EVENT_EMIT                  | Fully simulated | Sets mode bitmask from address byte                       |
| `0x09` | QUERY_GROUP_NUMBERS                    | Fully simulated | Sorted group numbers                                      |
| `0x0A` | QUERY_SCENE_NUMBERS                    | Not implemented | Legacy global scene list                                  |
| `0x0B` | QUERY_PROFILE_NUMBERS                  | Fully simulated | Packed BE16 ids                                           |
| `0x0C` | QUERY_OCCUPANCY_INSTANCE_TIMERS        | Fully simulated | deadtime/hold/report + wall-clock last_detect             |
| `0x0D` | QUERY_INSTANCES_BY_ADDRESS             | Simulated       | Type/status; state `0x00`; PB/occ only in YAML            |
| `0x0E` | DALI_COLOUR                            | Fully simulated | Colour frame; XY/Tc/RGBWAF; optional arc; group/broadcast |
| `0x10` | DMX_COLOUR                             | Not implemented | DMX frame path absent                                     |
| `0x12` | QUERY_GROUP_BY_NUMBER                  | Not implemented |                                                           |
| `0x13` | QUERY_SCENE_BY_NUMBER                  | Not implemented |                                                           |
| `0x14` | QUERY_SCENE_NUMBERS_BY_ADDRESS         | Not implemented |                                                           |
| `0x15` | QUERY_GROUP_MEMBERSHIP_BY_ADDRESS      | Fully simulated | Membership bitmap                                         |
| `0x16` | QUERY_DALI_ADDRESSES_WITH_INSTANCES    | Fully simulated | ECD wires; paged from start                               |
| `0x17` | QUERY_DMX_DEVICE_NUMBERS               | Not implemented |                                                           |
| `0x18` | QUERY_DMX_DEVICE_BY_NUMBER             | Not implemented |                                                           |
| `0x19` | QUERY_DMX_LEVEL_BY_CHANNEL             | Not implemented |                                                           |
| `0x1A` | QUERY_SCENE_NUMBERS_FOR_GROUP          | Fully simulated | Scene bitmask                                             |
| `0x1B` | QUERY_SCENE_LABEL_FOR_GROUP            | Fully simulated | Scenes 0–11                                               |
| `0x1C` | QUERY_CONTROLLER_VERSION_NUMBER        | Fully simulated | From YAML `version`                                       |
| `0x1D` | QUERY_CONTROL_GEAR_DALI_ADDRESSES      | Fully simulated | ECG presence bitmap                                       |
| `0x1E` | QUERY_SCENE_LEVELS_BY_ADDRESS          | Simulated       | 16 bytes; slots 0–11 live, 12–15 `0xFF`                   |
| `0x20` | QUERY_DMX_DEVICE_LABEL_BY_NUMBER       | Not implemented |                                                           |
| `0x21` | QUERY_INSTANCE_GROUPS                  | Not implemented |                                                           |
| `0x22` | QUERY_DALI_FITTING_NUMBER              | Not implemented |                                                           |
| `0x23` | QUERY_DALI_INSTANCE_FITTING_NUMBER     | Not implemented |                                                           |
| `0x24` | QUERY_CONTROLLER_LABEL                 | Fully simulated |                                                           |
| `0x25` | QUERY_CONTROLLER_FITTING_NUMBER        | Not implemented |                                                           |
| `0x26` | QUERY_IS_DALI_READY                    | Fully simulated | `OK` / `NO_ANSWER` from YAML                              |
| `0x27` | QUERY_CONTROLLER_STARTUP_COMPLETE      | Fully simulated | `OK` / `NO_ANSWER` from YAML                              |
| `0x28` | QUERY_OPERATING_MODE_BY_ADDRESS        | Not implemented |                                                           |
| `0x29` | OVERRIDE_DALI_BUTTON_LED_STATE         | Not implemented |                                                           |
| `0x30` | QUERY_LAST_KNOWN_DALI_BUTTON_LED_STATE | Not implemented |                                                           |
| `0x31` | DALI_ADD_TPI_EVENT_FILTER              | Fully simulated | Merges mask; reply `OK`                                   |
| `0x32` | QUERY_DALI_TPI_EVENT_FILTERS           | Fully simulated | Mode + up to 15 filters; empty → `NO_ANSWER`              |
| `0x33` | DALI_CLEAR_TPI_EVENT_FILTERS           | Fully simulated | `OK` if changed else `NO_ANSWER`                          |
| `0x34` | QUERY_DALI_COLOUR                      | Fully simulated | Tc / RGBWAF / XY                                          |
| `0x35` | QUERY_DALI_COLOUR_FEATURES             | Fully simulated | Feature byte from world                                   |
| `0x36` | SET_SYSTEM_VARIABLE                    | Simulated       | IDs 0–147 already in YAML; emits event mag=0              |
| `0x37` | QUERY_SYSTEM_VARIABLE                  | Fully simulated | Signed BE16                                               |
| `0x38` | QUERY_DALI_COLOUR_TEMP_LIMITS          | Fully simulated |                                                           |
| `0x40` | SET_TPI_EVENT_UNICAST_ADDRESS          | Fully simulated | Dynamic frame; `0.0.0.0`/port 0 clears                    |
| `0x41` | QUERY_TPI_EVENT_UNICAST_ADDRESS        | Fully simulated | mode + port + IPv4                                        |
| `0x42` | QUERY_SYSTEM_VARIABLE_NAME             | Fully simulated | Empty → `NO_ANSWER`                                       |
| `0x43` | QUERY_PROFILE_INFORMATION              | Not implemented |                                                           |
| `0x44` | QUERY_COLOUR_SCENE_MEMBERSHIP_BY_ADDR  | Fully simulated | Scene indices with colour data                            |
| `0x45` | QUERY_COLOUR_SCENE_0_7_DATA_FOR_ADDR   | Fully simulated | 8×7-byte blobs; unused = `0xFF`×7                         |
| `0x46` | QUERY_COLOUR_SCENE_8_11_DATA_FOR_ADDR  | Fully simulated | 4×7-byte blobs                                            |
| `0xA0` | DALI_INHIBIT                           | Simulated       | Timed inhibit flag; no sensor→load automation             |
| `0xA1` | DALI_SCENE                             | Fully simulated | Scenes 0–11; member level/colour events; broadcast 255    |
| `0xA2` | DALI_ARC_LEVEL                         | Fully simulated | Mutates + `LEVEL_CHANGE_V2`; unknown → `0xB8`             |
| `0xA3` | DALI_ON_STEP_UP                        | Fully simulated | On-if-off + step                                          |
| `0xA4` | DALI_STEP_DOWN_OFF                     | Fully simulated | Off-at-min                                                |
| `0xA5` | DALI_UP                                | Fully simulated | No ignite from off                                        |
| `0xA6` | DALI_DOWN                              | Fully simulated | Clamps at min                                             |
| `0xA7` | DALI_RECALL_MAX                        | Fully simulated | Per-member max                                            |
| `0xA8` | DALI_RECALL_MIN                        | Fully simulated | Per-member min                                            |
| `0xA9` | DALI_OFF                               | Fully simulated | Level 0                                                   |
| `0xAA` | DALI_QUERY_LEVEL                       | Simulated       | ECG/group; mid-fade interpolates; not broadcast 255       |
| `0xAB` | DALI_QUERY_CONTROL_GEAR_STATUS         | Fully simulated | ECG / group / broadcast 255 OR                            |
| `0xAC` | DALI_QUERY_CG_TYPE                     | Fully simulated | 32-bit LE type mask                                       |
| `0xAD` | DALI_QUERY_LAST_SCENE                  | Fully simulated | ECG or group                                              |
| `0xAE` | DALI_QUERY_LAST_SCENE_IS_CURRENT       | Fully simulated | Cleared by level/colour on members                        |
| `0xAF` | DALI_QUERY_MIN_LEVEL                   | Fully simulated |                                                           |
| `0xB0` | DALI_QUERY_MAX_LEVEL                   | Fully simulated |                                                           |
| `0xB1` | DALI_QUERY_FADE_RUNNING                | Fully simulated | Status bit `0x10`                                         |
| `0xB2` | DALI_ENABLE_DAPC_SEQ                   | Stub            | Always `NO_ANSWER`; no 250 ms DAPC override               |
| `0xB3` | VIRTUAL_INSTANCE                       | Not implemented |                                                           |
| `0xB4` | DALI_CUSTOM_FADE                       | Fully simulated | Fade seconds BE16; query interpolates; V2 = destination   |
| `0xB5` | DALI_GO_TO_LAST_ACTIVE_LEVEL           | Fully simulated | Per-member; fallback 254                                  |
| `0xB6` | QUERY_VIRTUAL_INSTANCES                | Not implemented |                                                           |
| `0xB7` | QUERY_DALI_INSTANCE_LABEL              | Fully simulated | ECD wire + instance in data lo                            |
| `0xB8` | QUERY_DALI_EAN                         | Not implemented |                                                           |
| `0xB9` | QUERY_DALI_SERIAL                      | Fully simulated | 8-byte BE serial ECG/ECD                                  |
| `0xC0` | CHANGE_PROFILE_NUMBER                  | Fully simulated | `0xFFFF` → last scheduled; emits profile event            |
| `0xC1` | DALI_STOP_FADE                         | Fully simulated | Freezes mid-fade + `LEVEL_CHANGE_V2`                      |


Deliberate deviations: broadcast wire **255** only (not **127**); level events
are `**LEVEL_CHANGE_V2` only**; scene level queries return 16 DALI slots with
12–15 as `0xFF`; colour-scene unused slots are `0xFF` × 7.

#### Events


| Code   | Event                         | Status          | Extent                                |
| ------ | ----------------------------- | --------------- | ------------------------------------- |
| `0x00` | BUTTON_PRESS_EVENT            | Event-only      | Inject via `-i`; ECD wire `64+ecd`    |
| `0x01` | BUTTON_HOLD_EVENT             | Event-only      | Inject via `-i`                       |
| `0x02` | ABSOLUTE_INPUT_EVENT          | Not implemented |                                       |
| `0x03` | LEVEL_CHANGE_EVENT            | Not implemented | Deliberate omission                   |
| `0x04` | GROUP_LEVEL_CHANGE_EVENT      | Not implemented | Deliberate omission                   |
| `0x05` | SCENE_CHANGE_EVENT            | Event-only      | Emitted on `DALI_SCENE` / inject      |
| `0x06` | OCCUPANCY_EVENT               | Event-only      | Inject + optional discovery heartbeat |
| `0x07` | SYSTEM_VARIABLE_CHANGED_EVENT | Event-only      | On `SET_SYSTEM_VARIABLE`              |
| `0x08` | COLOUR_CHANGED_EVENT          | Event-only      | On colour / colour scenes             |
| `0x09` | PROFILE_CHANGED_EVENT         | Event-only      | On profile change / inject            |
| `0x0A` | GROUP_OCCUPANCY_EVENT         | Not implemented |                                       |
| `0x0B` | LEVEL_CHANGE_EVENT_V2         | Event-only      | Sole level-change event used          |


