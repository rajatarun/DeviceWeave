# Codebook: formalising AutoTap Study 1 statements (v1.1)

This codebook turns each Study 1 statement into a small formal property that
does not depend on DeviceWeave's DSL. Two people code every statement
independently, `scripts/property_coding.py agree` measures their agreement,
disagreements are adjudicated, and the result is the ground truth that every
number in the paper comes from.

Freeze this file before coding starts: record the commit hash in the paper. A
change after the pilot creates a new version, and statements coded under the
old version are re-checked. v1.1 added the `mixed` actor and the rule for
regulated quantities, before any statement was coded.

The dataset is Study 1 of AutoTap (Zhang, He, Martinez, Brackenbury, Lu, Ur,
ICSE 2019, <https://ieeexplore.ieee.org/abstract/document/8811900>; data at
<https://github.com/zlfben/autotap>). It is not redistributed. The worksheet
that shows coders the statements is written to the git-ignored
`benchmarks/autotap/_out/`, and the code files hold only the fields below,
keyed by spreadsheet position. Never copy statement text into a code, a note,
or anything committed.

## The unit

One statement, one row: `study1:Result:row<r>:stmt<k>`. Code what the
statement says, not what the participant probably meant. When a statement
makes two independent demands, code the first one and set `multi_condition`
only if the extra demand is a *condition*; a second independent demand is a
`note`.

The examples below are invented for this codebook.

## Fields

### `scope` — is it a property at all?

| Value | Use when | Example |
|---|---|---|
| `property` | It constrains what a device or the home should or should not do, in a way one could check | "No cooking appliance may run while the flat is empty." |
| `not_a_property` | Anything else; then also set `not_property_reason` and nothing else | "My devices should be easier to set up." |

`not_property_reason`: `vague` (no checkable condition: "lights should be
smart"), `preference` (taste with no rule: "I like warm light"),
`about_product` (setup, apps, reliability, privacy policy), `not_home_automation`
(not about the home's devices), `other`.

### `kind` — AutoTap's three property shapes

| Value | AutoTap template | Example |
|---|---|---|
| `state` | *This state* should always/never be active (optionally *for more than this long*, *while that*) | "The heater should never be on while we are away." |
| `state_pair` | *This state and that state* should always/never occur together | "The AC and an open window should never occur together." |
| `event` | *This event* should always/never happen (optionally *while that*, *within this long after that*) | "The front door should lock within five minutes after the last person leaves." |

Choose `event` only when the statement is about something *happening* (a
transition, an alarm sounding, a lock engaging). "The door should be locked at
night" is a `state`.

### `modality`

`always` or `never`, as the statement says it, independent of the survey's
always/never category. Where the two disagree, code the statement and add a
`note`.

### `named_polarity` — which way the named state or event points

Every device state is classed by direction:

- **on-direction**: on, open, unlocked, running, playing.
- **off-direction**: off, closed, locked, stopped, idle.

A regulated quantity (temperature, humidity, water level): the named state
is the side of the bound the statement names, and "high" is on-direction.
"Never above 80" names *above 80* → `on`, `never`. "Always above 15" names
*above 15* → `on`, `always`. Its target is the regulating device
(`thermostat`, `kitchen_appliance`, …) with `target_actor: mixed`.

Security devices: "armed" counts as on-direction for an alarm or camera
(arming is an activation). Code the polarity of the state *the statement
names*. "Keep the side gate latched" → `off`. "The porch light should
be on after dark" → `on`.

### `target_class` and `target_actor`

`target_class` is the device the statement constrains: `light`, `plug_outlet`,
`fan`, `ac`, `heater`, `thermostat`, `lock`, `door`, `window`, `garage`,
`blinds`, `camera_security_alarm`, `kitchen_appliance`, `laundry_appliance`,
`cleaning_robot`, `media_speaker`, `sensor`, `water_irrigation`, `multiple`,
`other`.

`target_actor` is who can change that target's state:

| Value | Meaning | Example |
|---|---|---|
| `system` | A smart-home system can command it, and nothing else changes it | smart lock, smart plug, connected light |
| `mixed` | The system can command it, and the world or a person also changes it on their own | room or fridge temperature, humidity, a smart faucet also turned by hand |
| `human` | Only a person, by hand | an ordinary window, a non-smart door |
| `world` | Nobody; it changes by itself | the temperature, a sensor reading |

When the statement does not say whether a device is smart, use `system` for a
device class that usually is (light, plug, lock, thermostat, garage) and
`human` for windows and ordinary doors.

### `condition` and `condition_actor`

The *while that* / *after that* part.

| `condition` | Example | usual `condition_actor` |
|---|---|---|
| `none` | "Kettle off, full stop." | — |
| `presence` | "while nobody is home", "when I arrive", "while asleep" | `world` |
| `time_of_day` | "after 10 pm", "at night", "after dark" | `world` |
| `day_date` | "on weekdays", "on holidays", "in winter" | `world` |
| `weather` | "when it rains", "when it's hot" | `world` |
| `device_state` | "while the window is open", "when the TV is on" | the other device's actor |
| `sensor_event` | "when motion is detected", "when smoke is detected" | `world` |
| `other` | anything else | your judgement |

`condition_actor` is required whenever `condition` is not `none`. A
`state_pair` statement always has `condition: device_state`, describing the
second state.

### Modifiers

| Field | `true` when |
|---|---|
| `duration` | the statement bounds how long something lasts ("for more than two hours") |
| `within_after` | an `event` statement allows a delay ("within five minutes after …") |
| `multi_condition` | more than one condition is ANDed or ORed ("after dark and when nobody is home") |
| `exception_in_text` | the statement itself says "unless" / "except" (separate from the survey's exceptions question) |
| `fits_autotap_template` | the statement can be written in one AutoTap template exactly, with no loss |

### `note`

Optional, short, in your own words. The tooling refuses a note that repeats
the statement.

## How codes become plants (for the paper's method section)

`scripts/property_coding.py` builds one abstract plant per coded property and
classifies it with the same synthesis DeviceWeave uses
(`policy_authoring.controllability.supcon_forcing`, Reniers & Cai,
arXiv:2404.08469, Algorithm 1, safety case). The abstraction is deliberate and
is a threat to validity:

- One boolean for the target (in its on-direction state or not), one boolean
  for the condition, and a 0–2 counter when `duration` or `within_after` is set.
  A `multi_condition` statement is reduced to one condition whose actor is the
  least controllable of its parts.
- `event`/`always` is treated as the post-state it produces having to hold
  (for example "lock when everyone leaves" as "locked while away"), with
  `within_after` giving one extra step of grace.
- `event`/`never` forbids the transition itself; once it has happened it
  cannot be undone.
- **Reaction** (an assumption, default on): after an uncontrollable change
  makes the property false, the system gets one step to restore it before any
  further uncontrollable event. With reaction off, the system has to act
  before the change happens.
- **Manual** (an assumption, default off): a system device also has a manual
  switch the system cannot refuse.

The enforcement architectures compared:

| Architecture | Refuses on-direction commands | Refuses off-direction commands | Can force on-direction actions | Can force off-direction actions |
|---|---|---|---|---|
| `guard_dw` (DeviceWeave today) | yes | no | no | no |
| `guard_any` (a guard that refuses anything) | yes | yes | no | no |
| `guard_dw_obligation` (DeviceWeave + forced turn-off) | yes | no | no | yes |
| `tap` (trigger-action automation) | no | no | yes | yes |
| `guard_tap` (both) | yes | yes | yes | yes |

Each property gets one outcome per architecture:

| Outcome | Meaning |
|---|---|
| `exact` | a supervisor keeps every reachable legal state and the closed loop never violates the property |
| `preemptive` | as `exact`, but only by acting before anything has happened (over-actuation) |
| `over_restrictive` | a safe supervisor exists only by refusing things the property allows |
| `impossible` | no supervisor under this architecture keeps the property |
| `unsatisfiable` | no state satisfies the property at all |
