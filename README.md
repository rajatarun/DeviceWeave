# DeviceWeave

An AI-native execution layer that converts human intent into safe, semantic, real-time control of physical IoT environments.

```
"I'm starting work"          →  office light on, fan on
"it's too hot"               →  fan on
"dim the desk light to 40%"  →  office light → 40% brightness
"leaving"                    →  all devices off
```

DeviceWeave is not a smart home app or a voice assistant. It is an **AI execution substrate for the physical world**: LLMs generate intent, DeviceWeave guarantees safe execution.

---

## Architecture

```
POST /execute                       POST /ingest  ·  EventBridge schedule
     │                                    │
     ▼                                    ▼
 Scene Resolver ── conf ≥ 0.70 ──► Execution Planner   Ingestion Pipeline
     │ (nearest-neighbour              │ (asyncio.gather  ├── full sync (daily)
     │  phrase cosine)                 │  concurrent)     └── delta sync (6 h)
     │ below threshold                 ▼                        │
     ▼                          Provider Registry         Kasa Discovery
 Intent Parser                  ├── KasaAdapter           (UDP broadcast)
     │ (deterministic regex)    └── KasaAdapter                 │
     ▼                                    │               Secrets Manager
 Device Resolver ── conf ≥ 0.70 ──► Safety Layer         (kasa credentials)
     │ (TF cosine +                  ├── capability check        │
     │  learned phrases)             └── param validation        ▼
     │ below threshold                         │          DynamoDB registry
     ▼                                         ▼          (device fleet)
  422 Rejected                        Kasa LAN execution
                                               │
                                               ▼
                                      DynamoDB learning write
                                      (conf ≥ 0.85 only)
```

### Request flow

1. **Scene resolution** — nearest-neighbour cosine similarity against each individual sample phrase. Exact phrase matches score 1.0. Confidence ≥ 0.70 triggers the scene (potentially multi-device).
2. **Intent parsing** — deterministic regex parser extracts `action`, `device_query`, and `params`. No model calls, no network I/O.
3. **Device resolution** — TF-vector cosine similarity against the device catalog, augmented with phrases learned from prior successful executions.
4. **Safety layer** — confidence gate (< 0.70 → 422), capability validation, parameter validation. No device I/O until all checks pass.
5. **Execution** — routed through the provider registry to the correct protocol adapter. Scene steps run concurrently via `asyncio.gather`.
6. **Learning** — confidence ≥ 0.85 causes the normalized command to be written to DynamoDB as a new sample phrase for the resolved device.

---

## API

### Execution API

| Method | Path | Body | Description |
|--------|------|------|-------------|
| `POST` | `/execute` | `{"command": "..."}` | Execute a natural language device or scene command |
| `POST` | `/learn` | `{"device_id": "...", "phrase": "..."}` | Manually bind a phrase to a device |
| `GET` | `/health` | — | Liveness probe with device/scene counts and learning status |
| `GET` | `/devices` | — | List registered devices and their capabilities |
| `GET` | `/scenes` | — | List registered scenes and sample phrases |

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
    ├── execution_planner.py         Concurrent multi-device execution
    ├── learning_store.py            DynamoDB phrase learning store
    ├── kasa_provider.py             Compatibility shim → KasaAdapter
    ├── requirements.txt             Runtime dep: python-kasa==0.7.7
    ├── providers/                   Execution protocol adapters
    │   ├── __init__.py              Protocol registry
    │   ├── base.py                  BaseDeviceProvider ABC + ProviderError
    │   └── kasa_adapter.py          TP-Link Kasa LAN adapter
    └── ingestion/                   Device discovery and registry sync
        ├── __init__.py
        ├── pipeline.py              IngestionPipeline orchestrator (full / delta)
        ├── device_registry.py       DeviceRecord dataclass + DynamoDB operations
        └── providers/               Discovery provider adapters
            ├── __init__.py
            ├── base.py              AbstractDiscoveryProvider ABC
            └── kasa_discovery.py    Kasa UDP broadcast discovery
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

### Kasa credentials secret (one-time)

The ingestion Lambda reads Kasa account credentials from a single Secrets Manager secret. Create it before the first deploy:

```bash
aws secretsmanager create-secret \
  --name deviceweave/kasa-credentials \
  --region us-east-1 \
  --secret-string '{"email":"you@example.com","password":"yourpassword"}'
```

The secret ARN is injected automatically into the Lambda via `KASA_SECRET_ARN`. If the variable is absent (local dev), discovery falls back to unauthenticated mode (compatible with older Kasa firmware).

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

Four checks fire in sequence before any device I/O:

| # | Check | Failure response |
|---|-------|-----------------|
| 1 | Confidence ≥ 0.70 | `422` — low confidence, suggests `/learn` |
| 2 | Action in `device["capabilities"]` | `422` — unsupported action |
| 3 | `set_brightness` has a numeric value | `400` — missing parameter |
| 4 | Idempotency — device already in target state | `200` with `"changed": false` |

LLMs are not connected to the execution path. The only route to device I/O is through the deterministic parser → resolver → safety layer.

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
