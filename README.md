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
POST /execute
     │
     ▼
 Scene Resolver ── conf ≥ 0.70 ──────► Execution Planner
     │ (nearest-neighbour                   │  (asyncio.gather —
     │  phrase cosine)                      │   all steps concurrent)
     │ below threshold                      ▼
     ▼                               Provider Registry
 Intent Parser                       ├── KasaAdapter (SmartPlug)
     │ (deterministic regex)         └── KasaAdapter (SmartBulb)
     ▼                                         │
 Device Resolver ── conf ≥ 0.70 ──► Safety Layer
     │ (TF cosine +                  ├── capability check
     │  learned phrases)             └── param validation
     │ below threshold                         │
     ▼                                         ▼
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

| Method | Path | Body | Description |
|--------|------|------|-------------|
| `POST` | `/execute` | `{"command": "..."}` | Execute a natural language device or scene command |
| `POST` | `/learn` | `{"device_id": "...", "phrase": "..."}` | Manually bind a phrase to a device |
| `GET` | `/health` | — | Liveness probe with device/scene counts and learning status |
| `GET` | `/devices` | — | List registered devices and their capabilities |
| `GET` | `/scenes` | — | List registered scenes and sample phrases |

### Example requests

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

### Response shapes

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
└── src/
    ├── app.py                       Lambda handler — routing and dispatch
    ├── intent_parser.py             Deterministic regex intent parser
    ├── device_resolver.py           TF-cosine device resolver + catalog
    ├── scene_catalog.py             Scene catalog + nearest-neighbour resolver
    ├── execution_planner.py         Concurrent multi-device execution
    ├── learning_store.py            DynamoDB phrase learning store
    ├── kasa_provider.py             Compatibility shim → KasaAdapter
    ├── requirements.txt             Runtime dep: python-kasa==0.7.7
    └── providers/
        ├── __init__.py              Protocol registry
        ├── base.py                  BaseDeviceProvider ABC + ProviderError
        └── kasa_adapter.py         TP-Link Kasa LAN adapter
```

---

## Deployment

### Prerequisites

- AWS account with Lambda, API Gateway, DynamoDB, CloudFormation, IAM, S3 permissions
- Python 3.11
- Devices on the same LAN as the Lambda execution environment (or routed via VPC/VPN)

### Required GitHub Secrets

| Secret | Value |
|--------|-------|
| `AWS_ACCESS_KEY_ID` | IAM access key |
| `AWS_SECRET_ACCESS_KEY` | IAM secret key |
| `AWS_REGION` | e.g. `us-east-1` |

The IAM principal needs: `lambda:*`, `cloudformation:*`, `apigateway:*`, `logs:*`, `dynamodb:*`, `s3:*`, `iam:PassRole`.

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
