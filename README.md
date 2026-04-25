# DeviceWeave

An AI-native execution layer that converts human intent into safe, semantic, real-time control of physical IoT environments — with a policy authoring system that enforces automation rules at runtime before any device I/O.

```
"I'm starting work"          →  office light on, fan on
"it's too hot"               →  fan on
"dim the desk light to 40%"  →  office light → 40% brightness
"leaving"                    →  all devices off
```

Rules you can author:

```
"Don't turn on fan when it's cold"         →  blocked (temperature < 65°F)
"Don't turn on lights between 11am–3pm"   →  blocked (time_hour >= 11 and <= 15)
"Dim lights after 10 PM"                  →  params modified (brightness: 20)
```

DeviceWeave is not a smart home app or a voice assistant. It is an **AI execution substrate for the physical world**: LLMs generate intent, DeviceWeave enforces policy, hardware executes.

---

## Architecture

```
POST /execute                                POST /policies/author   POST /ingest · EventBridge
     │                                              │                      │
     ▼                                              ▼                      ▼
 Scene Resolver ── conf ≥ 0.70 ──►         LLM Policy Compiler     Ingestion Pipeline
     │ (nearest-neighbour                  (Claude Haiku 4.5)       ├── full sync (daily)
     │  phrase cosine)                            │                  └── delta sync (6 h)
     │ below threshold                     Policy Validator                │
     ▼                                     (schema + conf ≥ 0.85)   Kasa Discovery
 Intent Parser                                    │                  (UDP broadcast)
     │ (deterministic regex)               DynamoDB PolicyTable            │
     ▼                                     (versioned rules)         Secrets Manager
 Device Resolver ── conf ≥ 0.70 ──►               │
     │ (TF cosine +                  ┌────────────────────────────────────────┐
     │  learned phrases)             │         Policy Engine                  │
     │ below threshold               │  context_provider (temp·hum·time·home) │
     ▼                               │  policy_loader    (DynamoDB TTL cache) │
 LLM Resolver ── conf ≥ 0.70 ──►    │  evaluator        (condition matching)  │
     │ (Claude Haiku 4.5)            │  verdict: BLOCK → 403  no device I/O   │
     │ below threshold               │           MODIFY → updated params      │
     ▼                               │           ALLOW  → pass-through        │
  422 Rejected                       └────────────────────────────────────────┘
                                                   │
                                             Safety Layer
                                             ├── capability check
                                             └── param validation
                                                   │
                                           Provider Registry
                                           ├── KasaAdapter
                                           ├── SwitchBotAdapter
                                           └── GoveeAdapter
                                                   │
                                           Device execution (LAN)
                                                   │
                                           DynamoDB learning write
                                           (conf ≥ 0.85 only)
```

### Execution request flow

1. **Scene resolution** — nearest-neighbour cosine similarity against each individual sample phrase. Confidence ≥ 0.70 triggers the scene (potentially multi-device).
2. **Intent parsing** — deterministic regex parser extracts `action`, `device_query`, and `params`. No model calls, no network I/O.
3. **Device resolution** — TF-vector cosine similarity against the device catalog, augmented with phrases learned from prior successful executions.
4. **LLM resolver (Tier 2)** — invoked only when Tier 1 cosine falls below 0.70. Claude Haiku 4.5 resolves contextual and behavioural commands using weather data and device roster.
5. **Policy Engine** — evaluates the resolved `(device_type, action)` pair against active Policy DSL rules stored in DynamoDB. BLOCK returns 403 with no I/O. MODIFY updates params before execution.
6. **Safety layer** — capability gate, parameter validation, idempotency check. No device I/O until all checks pass.
7. **Execution** — routed through the provider registry. Scene and multi-device steps run concurrently via `asyncio.gather`.
8. **Learning** — confidence ≥ 0.85 writes the normalized command to DynamoDB as a new sample phrase for the resolved device.

---

## API

### Execution API

| Method | Path | Body | Description |
|--------|------|------|-------------|
| `POST` | `/execute` | `{"command": "..."}` | Execute a natural language device or scene command |
| `POST` | `/learn` | `{"device_id": "...", "phrase": "..."}` | Manually bind a phrase to a device |
| `POST` | `/presence` | `{"is_home": true\|false}` | Update home-occupancy state for the Policy Engine |
| `GET` | `/health` | — | Liveness probe with device/scene counts and learning status |
| `GET` | `/devices` | — | List registered devices and their capabilities |
| `GET` | `/scenes` | — | List registered scenes and sample phrases |

### Policy Authoring API

| Method | Path | Body / Query | Description |
|--------|------|------|-------------|
| `POST` | `/policies/author` | `{"rule": "..."}` | Compile NL rule → Policy DSL → validate → store |
| `GET` | `/policies` | `?device_type=fan&limit=50` | List active policies (optional device_type filter) |
| `GET` | `/policies/{rule_id}` | `?version=2` | Get a specific policy (latest version by default) |
| `DELETE` | `/policies/{rule_id}` | — | Deactivate a policy |

### Ingestion API

| Method | Path | Body | Description |
|--------|------|------|-------------|
| `POST` | `/ingest` | `{"provider": "kasa", "mode": "full" \| "delta"}` | Trigger device discovery and sync to DynamoDB registry |

#### Sync modes

| Mode | Behaviour |
|------|-----------|
| `full` | Discovers all devices on the network, upserts every record, and marks any previously-active device not found this run as `offline`. Used for daily reconciliation. |
| `delta` | Discovers all devices, but only writes records whose fingerprint (SHA-256 of ip + name + model + mac) has changed. Unchanged devices get a `last_seen` touch only. Never marks offline. Used for frequent background polls. |

`mode` defaults to `delta` if omitted. `provider` defaults to `kasa` if omitted.

Schedules run automatically via EventBridge:
- **Full sync** — once per day (`rate(24 hours)`)
- **Delta sync** — every 6 hours (`rate(6 hours)`)

#### Example requests

```bash
# On-demand full sync — discover all Kasa devices and reconcile registry
curl -X POST $API_URL/ingest \
  -H "Content-Type: application/json" \
  -d '{"provider": "kasa", "mode": "full"}'

# On-demand delta sync — only write changed records
curl -X POST $API_URL/ingest \
  -H "Content-Type: application/json" \
  -d '{"provider": "kasa", "mode": "delta"}'
```

#### Response (202 Accepted)

```json
{
  "provider": "kasa",
  "mode": "full",
  "discovered": 4,
  "upserted": 3,
  "unchanged": 0,
  "offline": 1,
  "errors": 0,
  "duration_ms": 3241.7
}
```

| Field | Description |
|-------|-------------|
| `discovered` | Devices that responded to the UDP broadcast and were probed successfully |
| `upserted` | Records written or overwritten in DynamoDB |
| `unchanged` | Records whose fingerprint matched — `last_seen` updated only (delta mode) |
| `offline` | Active registry entries not found this run, now marked `offline` (full mode) |
| `errors` | Devices that responded to broadcast but failed during `update()` probe |
| `duration_ms` | Total wall time including network discovery |

### Execution example requests

```bash
# Single device command
curl -X POST $API_URL/execute \
  -H "Content-Type: application/json" \
  -d '{"command": "turn on the office light"}'

# Scene command (multi-device, concurrent)
curl -X POST $API_URL/execute \
  -H "Content-Type: application/json" \
  -d '{"command": "starting work"}'

# Brightness control
curl -X POST $API_URL/execute \
  -H "Content-Type: application/json" \
  -d '{"command": "dim the desk light to 40%"}'

# Teach the system a new phrase
curl -X POST $API_URL/learn \
  -H "Content-Type: application/json" \
  -d '{"device_id": "office_light", "phrase": "my reading lamp"}'
```

### Execution response shapes

**Device command**
```json
{
  "type": "device",
  "device_id": "office_light",
  "device_name": "Office Light",
  "action": "set_brightness",
  "confidence": 0.8135,
  "result": {"brightness": 40, "changed": true}
}
```

**Scene command**
```json
{
  "type": "scene",
  "scene_id": "work_mode",
  "scene_name": "Work Mode",
  "confidence": 1.0,
  "results": [
    {"device_id": "office_light", "action": "turn_on", "success": true, "result": {"state": "on", "changed": true}},
    {"device_id": "office_fan",   "action": "turn_on", "success": true, "result": {"state": "on", "changed": true}}
  ],
  "succeeded": 2,
  "failed": 0
}
```

**Low confidence rejection (422)**
```json
{
  "error": "No device matched with sufficient confidence (best=0.1162, threshold=0.7).",
  "best_match_id": "office_fan",
  "confidence": 0.1162,
  "hint": "Use POST /learn to add new phrases for a device."
}
```

### Policy authoring example

```bash
# Author a rule — natural language compiled by LLM, validated, stored
curl -X POST $API_URL/policies/author \
  -H "Content-Type: application/json" \
  -d '{"rule": "Don'\''t turn on the fan when it'\''s cold"}'
```

**Response (201 Created)**
```json
{
  "rule_id": "c3f8a2d1-7e45-4b9a-a312-8f1d2e3c4b5a",
  "version": 1,
  "scope": {"device_type": "fan"},
  "conditions": [{"field": "temperature", "operator": "<", "value": 65}],
  "action": {"type": "block", "reason": "Too cold to run fan", "params": {}},
  "confidence": 0.92,
  "source_text": "Don't turn on the fan when it's cold",
  "status": "active",
  "created_at": "2026-04-25T01:14:00.000000+00:00"
}
```

Once stored, any `/execute` call that resolves to a `fan` device with `turn_on` at < 65°F will receive:

```json
HTTP 403
{"error": "Policy blocked: Too cold to run fan", "rule_id": "c3f8a2d1-..."}
```

---

## Policy System

### Allowed Policy DSL schema

```json
{
  "rule_id": "auto",
  "scope": {
    "device_type": "fan | light | ac | plug | heater"
  },
  "conditions": [
    {
      "field": "temperature | humidity | time_hour | is_home",
      "operator": "> | < | >= | <= | == | !=",
      "value": "<number for temperature/humidity/time_hour  |  boolean for is_home>"
    }
  ],
  "action": {
    "type": "block | allow | modify",
    "reason": "<explanation>",
    "params": {}
  },
  "confidence": 0.0
}
```

| Field | Allowed values |
|-------|---------------|
| `scope.device_type` | `fan`, `light`, `ac`, `plug`, `heater` |
| `conditions[].field` | `temperature` (°F), `humidity` (%), `time_hour` (0–23), `is_home` (bool) |
| `conditions[].operator` | `>`, `<`, `>=`, `<=`, `==`, `!=` |
| `action.type` | `block` — prevent execution · `modify` — override params · `allow` — explicit permit |

### Authoring pipeline

```
Natural language rule
        │
        ▼  Claude Haiku 4.5 via Bedrock
LLM Policy Compiler   (system prompt enforces strict JSON output;
        │              explicit rejection on ambiguity or conf < 0.85)
        ▼
Policy Validator      (13-step deterministic check: whitelist, enums,
        │              type guards, confidence threshold)
        ▼
DynamoDB PolicyTable  (versioned — each update creates a new version row;
                       previous versions become "superseded", never deleted)
```

Rules with `confidence < 0.85` are rejected — the LLM is instructed to emit an explicit rejection object rather than guess.

### Runtime enforcement (Policy Engine)

On every `/execute` call the Policy Engine fires **after** device resolution and **before** any device I/O:

```
context_provider  →  {temperature: 60°F, humidity: 45%, time_hour: 14, is_home: true}
policy_loader     →  load active policies for device_type (60 s TTL cache)
evaluator         →  test all conditions (AND semantics per policy)
                     priority: BLOCK > MODIFY > ALLOW
```

**Verdict behaviour:**

| Verdict | HTTP | Behaviour |
|---------|------|-----------|
| `block` | 403 | Request rejected; `rule_id` and reason in response; zero device I/O |
| `modify` | 200 | Params replaced with policy `params`; `policy` field added to response |
| `allow` | 200 | Transparent pass-through; no response change |

**Safe-action bypass:** `turn_off` and `get_status` always bypass policy evaluation — deactivation is never restricted.

**Scene behaviour:** blocked steps are removed from the execution set and listed in `policy_blocks`. If all steps are blocked the request returns 403.

### Presence tracking

The `is_home` condition field is backed by a DynamoDB singleton. Update it before leaving or arriving home:

```bash
# Leaving
curl -X POST $API_URL/presence -H "Content-Type: application/json" -d '{"is_home": false}'

# Arriving
curl -X POST $API_URL/presence -H "Content-Type: application/json" -d '{"is_home": true}'
```

Defaults to `true` when not set — avoids accidental lockout from "nobody home" policies.

---

## Built-in devices

| ID | Name | Type | Capabilities |
|----|------|------|--------------|
| `office_light` | Office Light | SmartBulb | turn_on, turn_off, toggle, get_status, set_brightness |
| `office_fan` | Office Fan | SmartPlug | turn_on, turn_off, toggle, get_status |

IPs are configured in `src/device_resolver.py → DEVICE_CATALOG`.

## Built-in scenes

| ID | Trigger examples | Actions |
|----|-----------------|---------|
| `work_mode` | "starting work", "focus mode", "work from home" | light on, fan on |
| `cooling_mode` | "too hot", "need cooling", "warm in here" | fan on |
| `all_off` | "leaving", "goodnight", "heading out", "i'm done" | light off, fan off |
| `evening_mode` | "chill mode", "wind down", "relax mode" | light 30%, fan off |
| `presentation_mode` | "on a call", "video call", "presentation mode" | light 100%, fan off |

---

## Project structure

```
DeviceWeave/
├── template.yaml                    AWS SAM infrastructure
├── .github/
│   └── workflows/
│       └── deploy.yml               GitHub Actions CI/CD
├── docs/
│   └── oidc-trust-policy.json       IAM trust policy for GitHub OIDC role
└── src/
    ├── app.py                       Execution Lambda handler — routing and dispatch
    ├── ingestion_handler.py         Ingestion Lambda handler — API GW / EventBridge / direct
    ├── intent_parser.py             Deterministic regex intent parser
    ├── device_resolver.py           TF-cosine device resolver + catalog
    ├── scene_catalog.py             Scene catalog + nearest-neighbour resolver
    ├── decision_engine.py           Unified confidence scoring + intent classification
    ├── behavior_engine.py           Context-aware behavior scoring (Memgraph)
    ├── execution_planner.py         Concurrent multi-device execution
    ├── learning_store.py            DynamoDB phrase learning store
    ├── llm_resolver.py              Tier 2 LLM device resolver (Claude Haiku 4.5)
    ├── graph_engine.py              Memgraph behavior event persistence
    ├── weather_client.py            Open-Meteo weather client (cached daily)
    ├── kasa_provider.py             Compatibility shim → KasaAdapter
    ├── requirements.txt             Runtime deps: aiohttp, neo4j
    ├── policy_authoring/            Policy Authoring Lambda (POST /policies/*)
    │   ├── __init__.py
    │   ├── handler.py               Lambda handler — author / list / get / delete routes
    │   ├── llm_compiler.py          NL → Policy DSL via Claude Haiku 4.5 (Bedrock)
    │   ├── validator.py             13-step deterministic schema + confidence validator
    │   └── policy_store.py          DynamoDB PolicyTable CRUD with version history
    ├── policy_engine/               Runtime enforcement layer (injected in app.py)
    │   ├── __init__.py
    │   ├── context_provider.py      Runtime context: temp (°F), humidity, time, is_home
    │   ├── evaluator.py             Condition matching + BLOCK/MODIFY/ALLOW verdict
    │   ├── policy_loader.py         DynamoDB policy cache (60 s TTL, paginated scan)
    │   └── middleware.py            enforce() + filter_steps() — called by app.py
    ├── providers/                   Execution protocol adapters
    │   ├── __init__.py              Protocol registry
    │   ├── base.py                  BaseDeviceProvider ABC + ProviderError
    │   ├── kasa_adapter.py          TP-Link Kasa LAN adapter
    │   ├── switchbot_adapter.py     SwitchBot cloud adapter
    │   └── govee_adapter.py         Govee cloud adapter
    └── ingestion/                   Device discovery and registry sync
        ├── __init__.py
        ├── pipeline.py              IngestionPipeline orchestrator (full / delta)
        ├── device_registry.py       DeviceRecord dataclass + DynamoDB operations
        ├── phrase_generator.py      Bedrock-based sample phrase enrichment
        └── providers/               Discovery provider adapters
            ├── __init__.py
            ├── base.py              AbstractDiscoveryProvider ABC
            ├── kasa_discovery.py    Kasa UDP broadcast discovery
            ├── govee_discovery.py   Govee API discovery
            └── switchbot_discovery.py SwitchBot API discovery
```

---

## Deployment

### Prerequisites

- AWS account with Lambda, API Gateway, DynamoDB, CloudFormation, IAM, S3 permissions
- Python 3.11
- Devices on the same LAN as the Lambda execution environment (or routed via VPC/VPN)

### AWS OIDC setup (one-time, per account)

The CI/CD pipeline assumes an IAM role via GitHub OIDC — no long-lived access keys are stored in GitHub Secrets.

**Step 1 — Create the GitHub OIDC provider in your AWS account** (skip if it already exists):

```bash
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1
```

**Step 2 — Apply the trust policy to the deployer role:**

```bash
aws iam update-assume-role-policy \
  --role-name teamweave-github-actions-sam-deployer \
  --policy-document file://docs/oidc-trust-policy.json
```

The trust policy is at `docs/oidc-trust-policy.json`. It grants `sts:AssumeRoleWithWebIdentity` to the GitHub OIDC provider, restricted to the `rajatarun/DeviceWeave` repository via the `sub` claim.

**Step 3 — Attach the required permissions policy to the role:**

The role `arn:aws:iam::239571291755:role/teamweave-github-actions-sam-deployer` needs:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "lambda:*",
        "cloudformation:*",
        "apigateway:*",
        "logs:*",
        "dynamodb:*",
        "s3:*",
        "iam:PassRole",
        "iam:CreateRole",
        "iam:AttachRolePolicy",
        "iam:GetRole",
        "iam:DeleteRole",
        "iam:DetachRolePolicy",
        "iam:TagRole"
      ],
      "Resource": "*"
    }
  ]
}
```

### Device provider credentials (one-time per provider)

Each provider reads its credentials from a dedicated AWS Secrets Manager secret. Create the secrets before the first deploy. Providers whose secret is absent are silently skipped during ingestion — you only need to create secrets for providers you own devices from.

---

#### TP-Link Kasa

Kasa discovery uses the **Kasa cloud API** (not LAN broadcast), so it works from Lambda inside a VPC without any special routing.

**Step 1 — Find your credentials**

Use the email address and password you registered with in the **Kasa app** (iOS / Android). Two-factor authentication is not supported by the cloud API — if your account has 2FA enabled, create a sub-account without it.

**Step 2 — Create the secret**

```bash
aws secretsmanager create-secret \
  --name deviceweave/kasa-credentials \
  --region us-east-1 \
  --secret-string '{"email":"you@example.com","password":"yourpassword"}'
```

| Secret key | Value |
|------------|-------|
| `email` | The email address on your Kasa account |
| `password` | Your Kasa account password |

The Lambda reads this via `KASA_SECRET_ARN` (injected automatically by SAM). If the variable is absent, Kasa discovery is skipped.

---

#### Govee

Govee discovery uses the **Govee Developer API** (cloud, HTTPS). Devices must have cloud service enabled in the Govee Home app.

**Step 1 — Get a Govee API key**

1. Open the **Govee Home** app (iOS / Android).
2. Tap the profile icon → **Settings** (top right gear icon).
3. Scroll to **Apply for API Key**.
4. Enter your email address and a brief note (e.g. "Home automation").
5. Govee sends the API key to your email within a few minutes.

**Step 2 — Create the secret**

```bash
aws secretsmanager create-secret \
  --name deviceweave/govee-credentials \
  --region us-east-1 \
  --secret-string '{"api_key":"xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"}'
```

| Secret key | Value |
|------------|-------|
| `api_key` | The API key emailed to you by Govee |

**Supported device types discovered:**

| Govee device | DeviceWeave type | Capabilities added |
|---|---|---|
| Color Bulb, Strip Light, Ceiling Light | `GoveeBulb` | set_brightness, set_color, set_color_temp |
| Plug, Plug Mini, Smart Plug | `GoveePlug` | turn_on, turn_off, toggle, get_status |

Only devices with `controllable: true` in the Govee API response are registered. The Lambda reads this via `GOVEE_SECRET_ARN`.

---

#### SwitchBot

SwitchBot discovery uses the **SwitchBot Cloud API v1.1** with HMAC-SHA256 request signing. You need both a token and a secret key.

**Step 1 — Get your token and secret**

1. Open the **SwitchBot** app (iOS / Android).
2. Tap the profile icon (bottom right) → **Preferences**.
3. Tap **App Version** repeatedly (10 taps) until a "Developer Options" menu appears.
4. Tap **Developer Options**.
5. Copy both the **Token** and the **Client Secret**.

> The secret was added in SwitchBot API v1.1. If you only see a token (no secret), update the SwitchBot app to the latest version.

**Step 2 — Create the secret**

```bash
aws secretsmanager create-secret \
  --name deviceweave/switchbot-credentials \
  --region us-east-1 \
  --secret-string '{"token":"your-long-token-here","secret":"your-client-secret-here"}'
```

| Secret key | Value |
|------------|-------|
| `token` | The token from Developer Options |
| `secret` | The client secret from Developer Options |

**Supported device types discovered:**

| SwitchBot device | DeviceWeave type | Capabilities added |
|---|---|---|
| Color Bulb, Strip Light, Ceiling Light (Pro) | `SwitchBotBulb` | set_brightness, set_color, set_color_temp |
| Plug, Plug Mini (US/JP), Smart Plug | `SwitchBotPlug` | turn_on, turn_off, toggle, get_status |
| Fan, Ceiling Fan | `SwitchBotFan` | turn_on, turn_off, toggle, get_status |
| Curtain, Curtain3, Roller Shade | `SwitchBotCurtain` | turn_on, turn_off, toggle, get_status |

Devices with `enableCloudService: false` are skipped (they require BLE and cannot be reached from Lambda). The Lambda reads this via `SWITCHBOT_SECRET_ARN`.

---

#### Updating a secret

To rotate or correct a credential after the stack is already deployed:

```bash
# Kasa
aws secretsmanager put-secret-value \
  --secret-id deviceweave/kasa-credentials \
  --region us-east-1 \
  --secret-string '{"email":"new@example.com","password":"newpassword"}'

# Govee
aws secretsmanager put-secret-value \
  --secret-id deviceweave/govee-credentials \
  --region us-east-1 \
  --secret-string '{"api_key":"new-api-key"}'

# SwitchBot
aws secretsmanager put-secret-value \
  --secret-id deviceweave/switchbot-credentials \
  --region us-east-1 \
  --secret-string '{"token":"new-token","secret":"new-secret"}'
```

Secrets are cached per Lambda container. After updating, trigger a fresh sync to pick up the new credentials:

```bash
curl -X POST $API_URL/ingest \
  -H "Content-Type: application/json" \
  -d '{"provider": "kasa", "mode": "full"}'
```

### Required GitHub Secret

| Secret | Value |
|--------|-------|
| `AWS_REGION` | e.g. `us-east-1` |

`AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` are **not used** — credentials come from the OIDC role assumption.

### Manual deploy

```bash
pip install aws-sam-cli

sam build --use-container

sam deploy \
  --stack-name deviceweave-prod \
  --capabilities CAPABILITY_IAM \
  --region us-east-1 \
  --resolve-s3 \
  --parameter-overrides StageName=prod
```

The `ExecuteEndpoint` URL is in the stack outputs.

### GitHub Actions

Push to `main` or use `workflow_dispatch` (with a `stage` selector). The pipeline installs `aws-sam-cli` explicitly via pip, validates the template, builds with `--use-container`, and deploys with `--no-confirm-changeset`.

---

## Extending the system

### Add a device

Append to `DEVICE_CATALOG` in `src/device_resolver.py`:

```python
{
    "id": "standing_lamp",
    "name": "Standing Lamp",
    "ip": "192.168.1.103",
    "device_type": "SmartBulb",
    "capabilities": ["turn_on", "turn_off", "get_status", "toggle", "set_brightness"],
    "sample_phrases": ["standing lamp", "floor lamp", "corner light"],
}
```

### Add a scene

Append to `SCENE_CATALOG` in `src/scene_catalog.py`:

```python
{
    "id": "morning_mode",
    "name": "Morning Mode",
    "description": "Ease into the day.",
    "sample_phrases": ["good morning", "morning mode", "waking up"],
    "actions": [
        {"device_id": "office_light", "action": "set_brightness", "params": {"brightness": 60}},
        {"device_id": "office_fan",   "action": "turn_off",       "params": {}},
    ],
}
```

No other code changes required.

### Add a protocol (Matter, Zigbee, Thread)

1. Create `src/providers/matter_adapter.py` implementing `BaseDeviceProvider`
2. Register it in `src/providers/__init__.py`:

```python
from providers.matter_adapter import MatterAdapter
_matter = MatterAdapter()
for _device_type in MatterAdapter.supported_device_types():
    _REGISTRY[_device_type] = _matter
```

3. Set `"device_type": "MatterDevice"` on catalog entries handled by that adapter.

---

## Safety model

Six checks fire in sequence before any device I/O:

| # | Check | Failure response |
|---|-------|-----------------|
| 1 | Confidence ≥ 0.70 | `422` — low confidence, suggests `/learn` |
| 2 | Action in `device["capabilities"]` | `422` — unsupported action |
| 3 | `set_brightness` has a numeric value | `400` — missing parameter |
| 4 | **Policy Engine — BLOCK verdict** | `403` — blocked by active policy rule |
| 5 | **Policy Engine — MODIFY verdict** | params replaced, execution continues |
| 6 | Idempotency — device already in target state | `200` with `"changed": false` |

The Policy Engine is check 4–5: it fires after resolution (the system knows what device and action is intended) but before capability validation and device I/O.

LLM calls are bounded to two roles: (a) Tier 2 device resolution when cosine confidence fails, and (b) policy compilation via `/policies/author`. Neither LLM call can directly trigger device I/O — the deterministic safety layer and Policy Engine sit between them and the hardware.

---

## Continuous learning

Every successful execution with confidence ≥ 0.85 writes the normalized command to DynamoDB as a new sample phrase for the resolved device. On subsequent requests that phrase is included in the cosine similarity corpus.

- **Auto-learn threshold**: `LEARNING_CONFIDENCE_THRESHOLD` env var (default `0.85`)
- **Manual binding**: `POST /learn` — takes effect immediately (cache invalidated)
- **Cache**: learned phrases are held in Lambda container memory; refreshed on cold start or after a `/learn` write

---

## Local development

```bash
# Install runtime and dev dependencies
pip install python-kasa boto3

# Smoke test — no AWS credentials required
python3 -c "
import sys; sys.path.insert(0, 'src')
from intent_parser import parse_intent
from scene_catalog import resolve_scene
from device_resolver import resolve_device
print(parse_intent('turn on the office light'))
print(resolve_scene('starting work'))
print(resolve_device('turn on the office light'))
"

# Local Lambda invocation via SAM
sam build
sam local invoke DeviceWeaveFunction \
  --event '{"requestContext":{"http":{"method":"POST","path":"/execute"}},"body":"{\"command\":\"starting work\"}"}'
```

`boto3` is pre-installed in the Lambda Python 3.11 runtime and is not in `src/requirements.txt` (would add ~10 MB to the package). Install it locally only.
