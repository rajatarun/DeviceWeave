# AutoTap rule patterns and the DeviceWeave Policy DSL

This note compares the household-rule patterns described by AutoTap Study 1
with the Policy DSL DeviceWeave compiles and evaluates today.

Citation: Weijia He et al. (Lefan Zhang, Weijia He, Jesse Martinez, Noah
Brackenbury, Shan Lu, and Blase Ur), *AutoTap: Synthesizing and Repairing
Trigger-Action Programs Using LTL Properties*, IEEE/ACM ICSE 2019,
<https://ieeexplore.ieee.org/abstract/document/8811900>.
Artifact: <https://github.com/zlfben/autotap> (branch `master`).

The examples below are original "when X then Y" illustrations of each
pattern. They are not quotations from the study, and the study workbook is
not in this repository (GPL-3.0 code, no separate data licence found; see
`docs/autotap-study1.md`).

AutoTap's Study 1 coding (paper Section III and Table I) grouped desired
properties by three axes: state versus event, conditional versus
unconditional, and whether a duration is involved. Trigger-action programs
in the paper (Section II) have the shape "if an event occurs while some
states hold, then act." DeviceWeave is a different runtime: a person or
agent requests an action, and the policy engine allows, blocks, or rewrites
that request. It does not subscribe to device events or schedule actions.
Precedence stays **BLOCK over MODIFY over ALLOW**. `allow` policies are
informational and do not override a block. Conditions on one policy are
ANDed. Scope is a single `device_type` drawn from `fan`, `light`, `ac`,
`plug`, and `heater`. Condition fields are `temperature`, `humidity`,
`time_hour`, `cloud_cover_pct`, `is_home`, and `is_overcast`.

No Policy DSL or evaluator change is made for these gaps. Each uncovered
pattern needs a new runtime meaning (an event, a timer, another device's
state, an empty condition list, or a disjunction). Widening the schema
without that runtime would accept policies the evaluator cannot honor.

## Coverage

| Pattern | Example (original wording) | Coverage | Why | Suggested DSL change (not implemented) |
| --- | --- | --- | --- | --- |
| State condition on a supported field | When nobody is home, keep the heater off. | Covered | `heater`, `is_home == false`, `block`. The same shape covers temperature, humidity, cloud cover, and overcast. | None. |
| Clock time | Don't turn the lights on after 10 PM. | Covered | `time_hour >= 22` on `light`, `block`. The compiler prompt already maps several clock phrases onto `time_hour`. | None. `time_hour` is an hour of the day (0–23), not a duration. |
| Conjunction | When it is hot and nobody is home, block the plug. | Covered | Two conditions on one policy are both required. | None. |
| Simple exception that is still a conjunction | Don't run the fan when it is cold unless someone is home. | Partially | "Unless someone is home" is `temperature < 65` AND `is_home == false` on one block. An exception that is a disjunction ("unless it is humid or someone is home") cannot be one policy, and there is no `unless` clause. | An explicit `unless` list, or OR between condition groups. That changes AND-only matching in the evaluator and the region checker. |
| Modify | When it is after 9 PM, dim the lights. | Partially | `action.type` `modify` exists, and on a match the evaluator returns `action.params`. Params are an unchecked object. Nothing in the schema says what "dim" means, and the compiler prompt leaves `params` empty unless the wording carries explicit numbers. | Typed modify params (for example a brightness) checked by the validator. The evaluator would then have to interpret them, which is a behavior change. |
| Explicit allow | When I am home, the fan may run. | Partially | The DSL accepts `allow`. At runtime an allow does not turn the device on and does not override a block. | Leave precedence as it is. An allow that forced a device on would be a new actuation model. |
| Conflicting policies | Block the heater when nobody is home, and also allow the heater when nobody is home. | Covered | The engine applies BLOCK over MODIFY over ALLOW. `rule_set_checker` reports `conflict_block_allow` and shadowed modifiers. The compile-fidelity harness scores one statement at a time; conflict checking stays on the rule set. | None. |
| One-state unconditional ("always this state") | The porch light should always be on. | Not covered | A policy needs at least one condition, and no policy turns a device on by itself. `allow` does not hold a state. | An unconditional maintain-state action, plus a condition list that may be empty. Both change the validator and the evaluator. |
| One-event unconditional ("this never happens") | The heater should never turn on. | Not covered | A block with no condition is invalid, and there is no context field that is always true. | A block with an empty condition list, meaning "every request." That changes "at least one condition." |
| Duration | When the fan turns on, leave it on for at least 10 minutes. | Not covered | No timer field. `time_hour` does not measure how long a state has held. | A duration condition and a clock in the context provider. The region checker would need a new domain. |
| Multi-device state | The heater and the air conditioner should never be on together. | Not covered | Scope is one device type. Conditions cannot name another device's state. | A condition field whose value is another device's state, and context that carries those states. |
| Event while a state holds | When the window opens while the air conditioner is on, turn the air conditioner off. | Not covered | The engine gates a request already in progress. It does not see "window opens," and window is not a device type or a condition field. | An event trigger distinct from a state condition, plus device-state context. That is a second runtime beside the gate. |
| Event then event, with a deadline | When I arrive home, lock the door within one minute. | Not covered | No event order and no "within t" operator. A door lock is outside the device enum. | A followed-by operator with a bound, which the current interval model does not express. |
| Repeated schedule | Turn the porch light on every day at 7 AM. | Not covered | `time_hour == 7` can describe an hour if some other caller requests the action. Nothing fires on a period. | A scheduler. The policy engine would be actuating, not only gating. |
| Trigger-action actuation | If motion is detected, turn on the porch light. | Not covered | Same gap as an event trigger. Motion is not a condition field. The compiler prompt rejects conditions it cannot map. | Event triggers and a motion field. Out of scope for the gate. |
| Sensors and devices outside the enum | Don't turn the fan on when it rains. | Not covered | Rain, doors, sleep, cameras, and a thermostat setpoint are rejected on purpose. Device types stay `fan`, `light`, `ac`, `plug`, `heater`. | New device types and condition fields only if the context provider and the region checker grow with them. Adding the names alone would accept policies that never match. |

## What was left unchanged

The validator, the compiler prompt, the evaluator, and BLOCK > MODIFY > ALLOW
are unchanged. Patterns marked covered are already expressible. Patterns
marked partially are expressible only for the case that fits today's AND of
supported fields, or only as an action type whose parameters are unchecked.
Patterns marked not covered need a semantic extension; they are recorded
here so a later change can be scoped against this list rather than folded
into the compile-fidelity harness.
