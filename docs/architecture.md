# DeviceWeave — Architecture

> Current as of `template.yaml` and `src/`. `CLAUDE.md` holds the invariants,
> `DECISIONS.md` (41 numbered decisions) holds the reasons, and `README.md`
> documents the API, the policy DSL and deployment. This file describes the shape.

DeviceWeave is an AI execution substrate for the physical world. It switches real
devices (lights, fans, locks) in a home. **There is no undo.** Every design choice
below follows from that. LLMs generate intent. DeviceWeave enforces policy. Hardware
executes.

---

## The execution path

Every caller reaches the same function, `execution_planner._execute_one`. That is
the one place a gate covers every path.

```
caller ─┬─ MCP  /mcp  (execute, the 31 tools)
        ├─ REST /execute, /learn, /devices, … (behind rest_auth)
        ├─ SMS  (sms_handler)
        └─ conversational agents (gemini_agent, bedrock_agent)
             │
             ▼
  scene resolver ── regex intent parser (intent_parser, deterministic)
             │
             ▼
  device resolver ── TF cosine + learned phrases (device_resolver)
             │  below threshold
             ▼
  LLM resolver (tier 2, bounded to resolution only, llm_resolver)
             │
             ▼
  POLICY ENGINE  →  BLOCK / MODIFY / ALLOW  (policy_engine)
             │      Block > Modify > Allow; ALLOW never overrides BLOCK
             ▼
  safety layer: capability, parameters, idempotency
             │
             ▼
  provider registry (providers/): Kasa, SwitchBot, Govee, Ring, MyQ, Wyze
             │
             ▼
  learning write (learning_store) — only at confidence ≥ 0.85
```

Physical access is gated where the action happens (decision 40). `unlock`,
`open` and `toggle` on a door-type device need a step-up token, and the check is in
`_execute_one`. Step-up means the token's `iat` is at most 60 seconds old, and its
`jti` is spent once in a DynamoDB ledger. The ledger is shared across Lambda
instances, so a replay on another instance is refused.

---

## Invariants that shape the design

These are summarised here. The full wording and the reasons are in `CLAUDE.md`
and `DECISIONS.md`.

1. **No model call can actuate hardware.** Models are bounded to tier-2 resolution
   and to policy compilation. The Policy Engine and safety layer always sit between a
   model and a device.
2. **The intent parser is deterministic** (decision 3). Its action set is small and
   closed. No LLM is on the critical path.
3. **Policy authoring = LLM compiler + deterministic validator + rule-set analysis**
   (decisions 19, 20, 33). A rejected rule is `{"rejected": true}`. The validator
   checks the schema independently, and `rule_set_checker` rejects unsatisfiable
   rules and invariant violations (422).
4. **Verdict priority: Block > Modify > Allow** (decision 23).
5. **Context defaults fail safe, not closed** (decision 22). An unreachable sensor or
   presence table gives neutral values (70 °F, 50 %, `is_home=true`).
6. **Confidence gates are separate** (decision 9): scene, device and LLM thresholds,
   each falling back to `CONFIDENCE_THRESHOLD`. The LLM gate reads a self-reported
   confidence, so it has the highest bar.
7. **Physical access is step-up gated** (decision 40), see above.
8. **Every REST route except `GET /health` is authenticated** (decision 41).
   `GET /presence` reveals whether the house is empty, so it is never public.
9. **A tool with no declared policy is destructive** (decision 40).

---

## Components

### Compute

| Function | Handler | Runtime | Memory / timeout | Trigger |
|---|---|---|---|---|
| `deviceweave-handler-<stage>` | `app.handler` | Python 3.12 | 1024 MB / 120 s | HTTP API (`/execute`, `/learn`, `/devices`, `/scenes`, `/presence`, `/learnings`, `/providers`, `/health`, `/mcp`) |
| `deviceweave-ingestion-<stage>` | `ingestion_handler.handler` | Python 3.12 | 120 s | HTTP `/ingest`; scheduled full sync every 24 h and delta sync every 6 h |
| `deviceweave-eero-presence-<stage>` | `eero_presence_handler.handler` | Python 3.12 | — | `EeroPresencePollSchedule`, `rate(5 minutes)` by default |
| `deviceweave-eero-calibration-<stage>` | `eero_calibration_handler` | Python 3.12 | — | room calibration jobs |

Default function memory is 256 MB and timeout 30 s, overridden per function above.

### Storage (DynamoDB, on-demand)

| Table | Holds |
|---|---|
| `deviceweave-registry-<stage>` | device registry (`list`, `get`, `create`, `update`; delete marks inactive) |
| `deviceweave-phrases-<stage>` | learned device phrases, used by the device resolver |
| `deviceweave-presence-<stage>` | household presence |
| `deviceweave-eero-tracked-<stage>` | devices tracked through eero presence |
| `deviceweave-room-fingerprints-<stage>` | room calibration fingerprints |
| `deviceweave-room-checkins-<stage>` | labelled room check-ins |
| `deviceweave-room-schedule-<stage>` | room schedule rules |
| `deviceweave-policies-<stage>` | compiled policy rules |
| `deviceweave-scenes-<stage>` | scene catalog |
| `deviceweave-conversations-<stage>` | conversation history for `execute` with a `session_id` |

Plus the step-up `jti` ledger and the shared observatory metrics table. Spans go
to the shared `OBSERVATORY_METRICS` table under contract
(`tests/test_shared_table_contract.py`).

### Providers

Each provider has an adapter in `src/providers/` and a discovery module in
`src/ingestion/providers/`:

| Provider | Adapter | Discovery |
|---|---|---|
| Kasa | `kasa_adapter` | `kasa_discovery` |
| SwitchBot | `switchbot_adapter` | `switchbot_discovery` |
| Govee | `govee_adapter` | `govee_discovery` |
| Ring | `ring_adapter` | `ring_discovery` |
| MyQ | `myq_adapter` | `myq_discovery` |
| Wyze | `wyze_adapter` | `wyze_discovery` |

Discovery runs in the ingestion function and writes to the device registry. A
new provider is added in one place: register its adapter in `providers/__init__.py`
and its supported device types.

### Presence

Two sources feed `PresenceTable`:

- **Manual and tool calls** (`set_presence`).
- **eero** (`eero_presence.py`, `eero_client.py`). The poller calls the eero cloud
  API on the schedule and records which tracked devices are on the network. It is an
  unofficial API, so a change there breaks presence silently. Calibration
  (`eero_calibration_handler`, `room_calibration_client`, `room_fingerprint`) maps
  the signal to rooms. A room is named only from a labelled check-in, never guessed
  from a single signal (decision 37).

### LLM providers

The model is chosen by role, not by name, and the call goes through
`llm_provider/`:

| Provider | Module | Used for |
|---|---|---|
| Bedrock | `llm_provider/bedrock.py` | production; role pins from ModelWeave (`modelweave_roles.py`), cost via `bedrock_cost.py` |
| Gemini | `llm_provider/gemini.py` | conversational agent (`gemini_agent.py`) |
| Ollama | `llm_provider/ollama.py` | local development |

The LLM resolver (tier 2) and the policy compiler use the same provider layer.
Neither can call a device.

---

## MCP and REST

**MCP** (`/mcp`, `mcp_handler.py`) exposes 31 tools. Each tool has a scope
(`read`, `execute`, `destructive`) and a declared policy. A tool with no declared
policy is treated as destructive. `tools/list` shows only what the token can call.
Two protocol eras are served (`mcp_era.py`): the 2026-07-28 stateless revision and
the handshake revisions. Both are chosen per request, from the request, not from
earlier requests.

Tool groups:

| Group | Tools |
|---|---|
| Devices | `list_devices`, `get_device`, `create_device`, `update_device`, `delete_device` |
| Commands | `execute` |
| Presence | `get_presence`, `set_presence` |
| Rooms | `calibrate_room`, `list_room_fingerprints`, `delete_room_fingerprint`, `record_room_checkin`, `capture_room_reading`, `label_room_checkin`, `list_room_checkins`, `delete_room_checkin`, `set_room_schedule_rule`, `list_room_schedule_rules`, `delete_room_schedule_rule` |
| Scenes | `list_scenes`, `delete_scene` |
| Policies | `list_policies`, `get_policy`, `author_policy`, `delete_policy` |
| Learning | `get_learnings`, `learn_phrase`, `delete_learning` |
| Operations | `list_providers`, `ingest`, `health` |

**REST** routes are listed under *Compute*. Every route except `GET /health` goes
through `rest_auth.check`, which uses the same `BearerAuth` as `/mcp`, so the two
doors cannot disagree. `REST_AUTH_MODE` is `audit` while clients are being
granted scopes.

**Rollout modes.** `TOOL_SCOPE_MODE` and `REST_AUTH_MODE` can be `audit`. Step-up
and destructive routes are enforced in every mode. `AcceptLegacyToken` stays true
during migration.

---

## Shared code (vendored, never edited in place)

| Module | From | Pin |
|---|---|---|
| `src/cipherweave_verify/` | CipherWeave | `requirements-cipherweave.txt`, `VENDORED.json` |
| `src/modelweave_roles.py` | ModelWeave | `MODELWEAVE_VENDORED.json` |
| `src/bedrock_cost.py` | ModelWeave | `BEDROCK_COST_VENDORED.json` |
| `src/mcp_era.py` | identical in ContextWeave and ModelWeave | dual-era MCP |

---

## Known limits

- The eero presence source is unofficial. It can stop working without warning.
- Learning writes only at confidence ≥ 0.85, a separate threshold from the
  execution gates. Lower-confidence commands run but are not learned from.
- The policy compiler's silent error rate is measured by
  `scripts/policy_compile_bench.py`. Rules the validator accepts but that mean
  something else are not caught by the validator.
