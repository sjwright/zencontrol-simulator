# TPI Advanced documentation: conflicts and open questions

Findings from implementing a full simulator against *Advanced Third Party
Interface API Document* (20-11-2025). Each entry states the conflicting text,
what this simulator does, and a hardware test that would settle it.

Section 1 lists places where the document contradicts itself or another section.
Section 2 lists behaviour that is unstated and had to be guessed. Section 3 is a
single script that runs every test below.

Throughout, `A0`–`A63` are control gear, `G0`–`G15` are groups (wire `64+n`),
`ECD0`–`ECD63` are control devices (wire `64+n`).

---

## 1. Self-contradictions

### 1.1 Scene numbering: 0–11, 0–12, or 0–15?

Three different ranges appear:

| Location | Range implied |
| --- | --- |
| `QUERY_SCENE_LABEL`, `QUERY_SCENE_LABEL_FOR_GROUP` | "scene 0–12" (13 scenes) |
| `QUERY_SCENE_LEVELS_BY_ADDRESS` | 16 returned bytes (16 scenes) |
| `SCENE_CHANGE_EVENT` | scene byte, no stated ceiling |

These cannot all be right. Our reading is that Zencontrol exposes **12** scenes
(0–11) and that the 16-byte answer is the raw DALI gear scene table, whose slots
12–15 are unused. If so, "0–12" is an off-by-one in the label commands.

**Ask:** state the Zencontrol scene count once, and say explicitly that
`QUERY_SCENE_LEVELS_BY_ADDRESS` returns the 16-slot DALI table rather than 16
Zencontrol scenes.

```
for s in 0..15:
    r = DALI_SCENE(A0, s)
    print s, r.type, r.data        # first s that is REPLY_ERROR/INVALID_ARGS is the ceiling

levels = QUERY_SCENE_LEVELS_BY_ADDRESS(A0)
print len(levels), levels[12:16]   # expect 16 and 0xFF padding if the ceiling is 12
print QUERY_SCENE_LABEL_FOR_GROUP(G0, 12).type   # ANSWER here would prove 0-12 is real
```

### 1.2 `QUERY_GROUP_LABEL` with an empty label: ANSWER or NO_ANSWER?

The prose says an empty label replies `REPLY_ANSWER` with a data length of 0.
The worked example immediately below shows `REPLY_NO_ANSWER`.

We implement `REPLY_NO_ANSWER`, matching the example and the other label
queries. A zero-length `ANSWER` is also awkward for clients that treat
`data_length == 0` as a framing error.

**Ask:** delete whichever of the two is wrong.

```
# needs a group configured with a blank label
r = QUERY_GROUP_LABEL(G_blank)
assert r.type in (ANSWER, NO_ANSWER)
print r.type, r.data_length        # ANSWER+0 vs NO_ANSWER
```

### 1.3 `QUERY_DALI_DEVICE_LABEL` with an empty label: ERROR or NO_ANSWER?

`QUERY_DALI_DEVICE_LABEL` says an empty label replies `REPLY_ERROR`, while every
other label query in the document says `REPLY_NO_ANSWER`. `REPLY_ERROR` also
normally carries an error code, and none is specified here.

We reply `NO_ANSWER` for all label queries.

**Ask:** confirm whether device labels really differ, and if so give the error
code.

```
r = QUERY_DALI_DEVICE_LABEL(A_blank_label)
print r.type, r.data               # ERROR + which code? or NO_ANSWER?
# compare against a sibling query on the same target
print QUERY_DALI_INSTANCE_LABEL(ECD_blank, 0).type
```

### 1.4 Is wire address 127 a broadcast?

The addressing section lists both 127 and 255 as broadcast. Elsewhere 64–127 is
the control device range, which makes 127 `ECD63`. A single value cannot be both
the last control device and a broadcast.

We treat only 255 as broadcast and answer `ERROR_UNKNOWN_TARGET` for 127 on
gear commands.

**Ask:** confirm 127 is `ECD63` and remove it from the broadcast list, or
document the precedence rule.

```
DALI_ARC_LEVEL(A0, 0); DALI_ARC_LEVEL(A1, 0)
r = DALI_ARC_LEVEL(wire=127, level=254)
sleep 1
print r.type, QUERY_DALI_LEVEL(A0), QUERY_DALI_LEVEL(A1)
# levels at 254  -> 127 is broadcast
# ERROR/no change -> 127 is ECD63
```

### 1.5 `DALI_COLOUR` example uses colour type `0x00`

The RGBWAF worked example shows `0x00` in the colour type byte where the type
table gives RGBWAF as `0x80`. `0x00` is not a defined colour type.

**Ask:** correct the example to `0x80`.

```
DALI_COLOUR(A_rgb, level=254, bytes=[0x00, R,G,B,W,A,F])   # expect ERROR/ignored
DALI_COLOUR(A_rgb, level=254, bytes=[0x80, R,G,B,W,A,F])   # expect OK
print QUERY_DALI_COLOUR(A_rgb)
```

### 1.6 `DALI_COLOUR` says error data `0x01` means "busy"

The page ends with: "If there is an error response type and Data of `0x01` then
this is likely because the system was busy, so try again and it may work the next
time." But the error table defines `0x01` as `ERROR_CHECKSUM`. "Busy" matches
`ERROR_QUEUE_FAILURE` (`0xB3`) or `ERROR_CMD_REFUSED` (`0xB2`) instead.

A client that follows this advice will retry corrupt frames indefinitely and
never log a checksum problem.

**Ask:** name the code the controller actually returns when busy.

```
# hammer a colour fixture faster than the DALI bus can drain
for i in 0..200:
    r = DALI_COLOUR(A_rgb, 254, [0x80, i%256, 0,0,0,0,0])
    if r.type == ERROR: print i, hex(r.data[0]); break
# then confirm 0x01 really is a checksum failure
print send_raw(valid_frame_with_bad_checksum).data   # expect 0x01
```

---

## 2. Unstated behaviour

### 2.1 `COLOUR_CHANGED_EVENT` width for narrow fixtures

The document says: "If a fixture is just RGB or RGBW (and not RGBWAF) then the
data length will be equal to the number of channels + 1." So a 3-channel fixture
sends 4 bytes. But `QUERY_DALI_COLOUR` is only ever shown returning 7 bytes for
RGBWAF, and the width is not restated per event.

This matters: a client that only accepts 7-byte RGBWAF payloads silently drops
every colour event from RGB and RGBW fittings. We hit exactly that against
`zencontrol-python` and had to widen its parser.

We emit `channels + 1` on the event and keep `QUERY_DALI_COLOUR` at 7 bytes.

**Ask:** confirm the event narrows while the query does not, and state whether a
6-channel RGBWAF fixture always sends 7.

```
for fixture in [rgb_3ch, rgbw_4ch, rgbwaf_6ch]:
    subscribe COLOUR_CHANGED_EVENT
    DALI_COLOUR(fixture, level=254, bytes=[0x80, 10,20,30,40,50,60])
    e = await_event(COLOUR_CHANGED_EVENT, target=fixture)
    print fixture, e.data_length, hex_bytes(e.data)
    print "query:", len(QUERY_DALI_COLOUR(fixture).data)
```

### 2.2 `DALI_QUERY_LEVEL` on a group with mixed member levels

The document defines the unknown-target answer (0) but not what a group answers
when its members sit at different levels. Two conventions appear in the wild:
`255` as "mixed/unknown" (DALI's MASK) or the maximum member level.

We answer `255` here, but `QUERY_GROUP_BY_NUMBER` answers the maximum member
level, and the document does not reconcile the two.

**Ask:** state the group answer for mixed levels, for both commands.

```
DALI_ARC_LEVEL(A0, 100); DALI_ARC_LEVEL(A1, 200)   # both in G0
sleep 1
print QUERY_DALI_LEVEL(G0)          # 255? 200? 150?
print QUERY_GROUP_BY_NUMBER(0)      # same value, or the max?

DALI_ARC_LEVEL(G0, 120)             # now agreed
sleep 1
print QUERY_DALI_LEVEL(G0), QUERY_GROUP_BY_NUMBER(0)
```

### 2.3 `QUERY_DALI_ADDRESSES_WITH_INSTANCES` paging units

The paging advice — "run the command with start address 0 and then a further
command with start address 60 to check for instances on the final 4 control
devices" — only works if the start byte counts control devices (0–63), because
64 devices in pages of 60 leaves 4. But the answer contains wire addresses
(64–127), so request and response use different units in the same command.

We treat the start byte as a control device number.

**Ask:** say explicitly that the argument is a control device number while the
answer is wire addresses, or change one of them.

```
all   = QUERY_DALI_ADDRESSES_WITH_INSTANCES(start=0)
page2 = QUERY_DALI_ADDRESSES_WITH_INSTANCES(start=60)
print all, page2
# page2 empty on a small site is inconclusive: retry with start = (lowest ECD)+1
near  = QUERY_DALI_ADDRESSES_WITH_INSTANCES(start=lowest_ecd+1)
print near        # drops the lowest ECD -> device numbers
                  # unchanged             -> wire addresses
```

### 2.4 `DALI_QUERY_CG_TYPE` against a group or broadcast

The page says the command "does not work for groups or broadcast target" and
separately that a non-existent device "will return all zero". It does not say
which of those two applies to a group: an all-zero answer, or an error.

We answer all-zero for groups, broadcast and unknown gear alike.

**Ask:** state the reply for group and broadcast targets.

```
print QUERY_DALI_CG_TYPE(A_present)     # baseline, non-zero
print QUERY_DALI_CG_TYPE(A_absent)      # documented all-zero
print QUERY_DALI_CG_TYPE(G0)            # ANSWER 0x00000000, or ERROR?
print QUERY_DALI_CG_TYPE(broadcast)     # ANSWER 0x00000000, or ERROR?
```

### 2.5 `CHANGE_PROFILE_NUMBER` rejection code

The page specifies `REPLY_ERROR` with `ERROR_CMD_REFUSED` (`0xB2`) for a failure
to schedule, but does not distinguish a profile number that does not exist from
one that exists but is refused (for example while a higher-priority profile
holds). `ERROR_INVALID_ARGS` (`0xB1`) would be the natural code for the former.

We answer `CMD_REFUSED` for both.

**Ask:** say which code applies to an unknown profile number.

```
print CHANGE_PROFILE_NUMBER(0x7FFE).data      # unknown profile -> 0xB1 or 0xB2?
print CHANGE_PROFILE_NUMBER(valid_but_blocked).data
print CHANGE_PROFILE_NUMBER(0xFFFF).type      # documented: return to schedule
```

### 2.6 `GROUP_OCCUPANCY_EVENT` timer scope

The event is documented as state-change only, with a default 30 second "seconds
to unoccupied". Unstated: whether the timer is per group or per sensor when
several sensors target one group, and whether a trigger during the hold extends
the deadline or leaves the original one standing.

We keep one deadline per group and refresh it on every trigger.

**Ask:** state that the timer is per group and that triggers refresh it.

```
subscribe GROUP_OCCUPANCY_EVENT
trigger(sensor_a targeting G0);  print events()    # expect one [0xFF, 0x01]
trigger(sensor_a);               print events()    # expect none
sleep 20; trigger(sensor_b targeting G0)
sleep 15; print events()   # nothing yet -> refreshed; [0xFF,0x00] -> not refreshed
sleep 20; print events()   # expect [0xFF, 0x00] by now
```

### 2.7 `DALI_QUERY_LEVEL` mid-fade

Nothing says whether a query during a fade returns the instantaneous level or
the destination. `LEVEL_CHANGE_EVENT_V2` carries both, which suggests the
distinction matters elsewhere too.

We return the instantaneous interpolated level.

**Ask:** state which one the query reports.

```
DALI_ARC_LEVEL(A0, 0); sleep 1
DALI_CUSTOM_FADE(A0, level=254, seconds=10)
sleep 2; print QUERY_DALI_LEVEL(A0)    # ~50 -> instantaneous; 254 -> destination
sleep 12; print QUERY_DALI_LEVEL(A0)   # 254
```

---

## 3. Combined script

```
report = {}

# 1.1 scene ceiling
report.scene_ceiling = first(s for s in 0..15 if DALI_SCENE(A0, s).type == ERROR)
report.scene_levels  = QUERY_SCENE_LEVELS_BY_ADDRESS(A0)

# 1.2 / 1.3 empty labels
report.group_label  = QUERY_GROUP_LABEL(G_blank).type
report.device_label = QUERY_DALI_DEVICE_LABEL(A_blank).type

# 1.4 broadcast 127
DALI_ARC_LEVEL(A0, 0); sleep 1
report.wire127 = (DALI_ARC_LEVEL(wire=127, 254).type, QUERY_DALI_LEVEL(A0))

# 1.6 error 0x01 meaning
report.checksum_code = send_raw(valid_frame_with_bad_checksum).data

# 2.1 colour event width
for f in colour_fixtures:
    DALI_COLOUR(f, 254, [0x80,10,20,30,40,50,60])
    report.colour[f] = await_event(COLOUR_CHANGED_EVENT, f).data_length

# 2.2 mixed group level
DALI_ARC_LEVEL(A0,100); DALI_ARC_LEVEL(A1,200); sleep 1
report.mixed = (QUERY_DALI_LEVEL(G0), QUERY_GROUP_BY_NUMBER(0))

# 2.3 paging units
report.paging = (QUERY_DALI_ADDRESSES_WITH_INSTANCES(0),
                 QUERY_DALI_ADDRESSES_WITH_INSTANCES(lowest_ecd+1))

# 2.4 CG type on non-gear
report.cg_type = (QUERY_DALI_CG_TYPE(G0), QUERY_DALI_CG_TYPE(broadcast))

# 2.5 profile refusal
report.profile = CHANGE_PROFILE_NUMBER(0x7FFE).data

# 2.7 mid-fade query
DALI_ARC_LEVEL(A0,0); sleep 1; DALI_CUSTOM_FADE(A0, 254, 10); sleep 2
report.midfade = QUERY_DALI_LEVEL(A0)

print report
```
