# Architecture Decision Record — DeviceWeave

Technical decisions made during implementation, in the order they arose. Each entry records what was chosen, what was rejected, and why.

---

## 1. AWS HTTP API v2 over REST API

**Chosen**: `AWS::Serverless::HttpApi`
**Rejected**: `AWS::Serverless::Api` (REST API v1)

HTTP API v2 costs ~70% less per request, has lower latency, and supports the same routing primitives this system needs (method + path). REST API v1 provides additional features (request validation schemas, usage plans, WAF integration) that are not required at this stage and can be migrated to later if needed. The trade-off is explicit in template.yaml so future engineers can swap it without hunting for hidden wiring.

---

## 2. Stateless Lambda — no persistent connections

All device communication is opened and closed within the Lambda invocation boundary. A persistent connection pool would reduce Kasa handshake latency but would require a long-lived process (ECS, App Runner) and break the pay-per-request model that is a hard product requirement. Lambda execution environments do reuse between invocations, so the in-memory phrase cache (see decision 8) benefits from warm containers without violating the stateless contract.

---

## 3. Deterministic regex intent parser — no LLM call

**Chosen**: regex patterns in `intent_parser.py`
**Rejected**: calling an LLM to classify intent

An LLM call on the critical execution path adds 200–800 ms latency, costs money on every request, introduces a third-party dependency with its own failure modes, and requires prompt engineering that is hard to test deterministically. The set of supported actions (`turn_on`, `turn_off`, `toggle`, `get_status`, `set_brightness`) is small and closed — a regex classifier covers it completely and is tested in milliseconds. LLMs belong in the calling layer that constructs the `command` string, not inside the execution substrate.

---

## 4. Scene resolution: nearest-neighbour, not corpus cosine

**Chosen**: compute cosine similarity between the query and each individual sample phrase; take the maximum per scene.
**Rejected**: concatenate all phrases into one corpus string and compute a single similarity score.

The corpus approach fails for single-token trigger words. The math: for a query with 1 unique token matching a corpus with $n$ unique tokens (each appearing once), cosine similarity is $1 / \sqrt{n}$. To reach 0.70, the corpus can have at most 2 unique tokens — impossible for a real scene. With nearest-neighbour, a query of `"leaving"` scores 1.0 against the exact phrase `"leaving"` regardless of how many other phrases the scene contains. The algorithm is semantically correct: it answers "how similar is this query to the most similar known trigger?" rather than "how similar is it to the average of all triggers?". Device resolution retains the corpus approach because device queries are always multi-token after stop-word filtering.

---

## 5. Device resolution: TF-vector cosine similarity

**Chosen**: term-frequency vector cosine similarity with stop-word filtering
**Rejected**: exact string matching, keyword lookup, or calling an embedding API

Exact matching would fail on minor phrasing variation. Embedding APIs (OpenAI, Cohere, Bedrock) add external network dependency, variable latency, cost per token, and require key management — unacceptable for a safety-critical execution path. The TF approach is deterministic, explainable, testable offline, and — critically — has no runtime dependencies beyond the Python standard library. Stop-word filtering removes tokens that carry no device-identification signal (action verbs, function words) before vectorisation, which concentrates the similarity score on the tokens that actually identify the device.

---

## 6. Stop-word list covers action verbs

Action verbs parsed by `intent_parser` (`turn`, `switch`, `dim`, etc.) are also in the device resolver stop-word list. Without this, `"turn on the office light"` produces tokens `["turn", "office", "light"]` and `"turn"` inflates the vocabulary shared with both devices, diluting the cosine angle. After filtering, the query reduces to `["office", "light"]` — the signal tokens — and cosine similarity rises from ~0.67 to ~0.81.

The same stop-word list is duplicated in `device_resolver.py` and `scene_catalog.py` rather than imported from a shared module. This is intentional: `scene_catalog.py` must be importable without pulling in `device_resolver.py` (which imports `learning_store`, which imports `boto3`). Coupling the two through a shared tokenizer module would introduce an import chain that breaks local testing without AWS credentials. The duplication is eight lines; the decoupling is worth it.

---

## 7. Concurrent scene execution via asyncio.gather

Scene steps use `asyncio.gather(*tasks, return_exceptions=False)`. For a 2-device scene, total latency is `max(device_a_latency, device_b_latency)` rather than `device_a + device_b`. A sequential loop would be simpler but doubles latency for the common case. `return_exceptions=False` means a Python exception propagates immediately — but since each `_execute_one` coroutine catches all exceptions and returns a `StepResult`, the gather never sees a raw exception. Partial failures are reported in the results array; one failing device does not abort others.

---

## 8. In-memory learned-phrase cache

**Chosen**: module-level `_learned_phrases_cache: Optional[Dict]` populated on first use, retained until invalidated.
**Rejected**: DynamoDB read on every request; Redis/ElastiCache; no caching.

A DynamoDB read on every execution would add ~5–15 ms and a DynamoDB API call cost to every request. Redis adds infrastructure cost and operational complexity. The Lambda execution model partially solves the caching problem for free: warm containers reuse module-level state, so the cache lives for the container lifetime (typically several hours) without explicit TTL management. The cache is invalidated synchronously on `POST /learn` so manually added phrases are available on the very next request. Cold-start latency is paid once per container lifecycle, not per request.

---

## 9. Learning threshold at 0.85, execute threshold recommended at 0.70 (ships at 0.4)

**Execute threshold (0.70 recommended, 0.4 shipped default)**: below this the match is too uncertain to act on. The system returns a 422 with a hint to use `/learn`.

The shipped default is `0.4`, not `0.70`. That is a **testing** value: a fresh device catalog has no learned phrases, so a strict gate rejects nearly every command and the system looks broken before it has been trained. `0.70` is the value the design intends and the recommended production setting — these gates decide whether to actuate physical hardware, and there is no undo. The gap between the shipped default and the recommendation is a deliberate trade-off, not an oversight, and it is now stated wherever the number appears rather than only in this file.

**Three gates, not one.** The execute threshold is applied at three different decision points, which until now shared a single `CONFIDENCE_THRESHOLD` variable — so tightening the LLM tier also tightened scene matching. Each gate now reads its own variable and falls back to `CONFIDENCE_THRESHOLD` when unset:

| Variable | Gate | Why it may differ |
|---|---|---|
| `SCENE_CONFIDENCE_THRESHOLD` | scene resolution | a scene actuates several devices at once, so a wrong match is wider |
| `DEVICE_CONFIDENCE_THRESHOLD` | tier-1 device resolution | scored from the TF cosine and behaviour history, both local and auditable |
| `LLM_CONFIDENCE_THRESHOLD` | tier-2 LLM resolver | the model's *self-reported* confidence — a weaker signal than a measured cosine, and the one most worth holding to a higher bar |

**Learning threshold (0.85)**: a phrase is only persisted as a training example if the system was already confident. Persisting low-confidence matches would reinforce incorrect associations and degrade future resolution accuracy. The gap between the execute and learning thresholds is the "execute but don't learn" zone — the system will act but not treat the phrase as a reliable example.

All values are configurable via environment variables (`ConfidenceThreshold`, `SceneConfidenceThreshold`, `DeviceConfidenceThreshold`, `LlmConfidenceThreshold` and `LEARNING_CONFIDENCE_THRESHOLD` in template.yaml) so they can be tuned per stage without code changes.

---

## 10. DynamoDB ConditionalCheckFailedException for idempotent writes

`save_learned_phrase` uses `ConditionExpression="attribute_not_exists(phrase)"` on `put_item`. If the phrase is already stored, DynamoDB raises `ConditionalCheckFailedException` (caught via `botocore.exceptions.ClientError`). The handler then increments `use_count` via `update_item`. This approach avoids a read-before-write and is safe under concurrent Lambda invocations — two containers writing the same phrase simultaneously will have one succeed and one fall through to increment. The alternative (scan for existence first) is not atomic and costs an extra read unit.

---

## 11. Provider registry pattern

**Chosen**: `dict[str, BaseDeviceProvider]` keyed by `device_type` string, built at module load time.
**Rejected**: `if device_type == "SmartPlug": ... elif device_type == "SmartBulb": ...` chains in the execution path.

The if/elif chain is the simplest implementation but scatters protocol knowledge across the codebase and requires modifying the execution planner to add a new protocol. The registry keeps all protocol knowledge inside `providers/`. Adding Matter or Zigbee is three steps: create an adapter, register it in `providers/__init__.py`, add `device_type` to catalog entries. The execution planner, safety layer, and app handler do not change.

---

## 12. boto3 pinned in requirements.txt (reversed: it used to be excluded)

**Original decision**: `boto3` is pre-installed in the Lambda Python 3.11 runtime. Including it in `requirements.txt` causes SAM to bundle it into the deployment package, adding approximately 10 MB, so it was deliberately excluded and developers installed it locally with `pip install boto3`.

**Current state**: `src/requirements.txt` pins `boto3>=1.26.0`. The pin was added in commit `d5c11cc`, which adopted MCP Observatory for Bedrock telemetry — the observatory path writes spans to DynamoDB, and the change declared the SDK it writes with rather than relying on whatever version the runtime happens to ship. The ~10 MB package cost above is real and was accepted at that point; this file simply kept describing the world before it.

Worth knowing if the trade-off is ever revisited: `mcp-observatory` itself does not require `boto3` (it declares only `asyncpg`, and imports `boto3` lazily inside its optional `aws` extra), and DeviceWeave's own DynamoDB writes go through `aws_clients.get_dynamodb_resource()`. So the pin is a version floor and an explicit statement of intent, not a hard dependency of the observatory library. Dropping it would restore the 10 MB but return the deployment to whatever `boto3` the runtime provides.

The lazy `import boto3` inside `learning_store._table()` is unrelated to packaging: it keeps the SDK off the cold-start import path for requests that never touch the learning table.

---

## 13. Explicit CloudWatch log group in template.yaml

```yaml
DeviceWeaveFunctionLogGroup:
  Type: AWS::Logs::LogGroup
  Properties:
    LogGroupName: !Sub "/aws/lambda/deviceweave-handler-${StageName}"
    RetentionInDays: 14
```

Without this, Lambda auto-creates a log group with no retention policy. Logs accumulate indefinitely at standard CloudWatch Logs pricing. The explicit resource ties the log group to the CloudFormation stack lifecycle: it is deleted on `sam delete` and its retention policy is enforced from first invocation.

---

## 14. PointInTimeRecovery enabled on DynamoDB table

```yaml
PointInTimeRecoverySpecification:
  PointInTimeRecoveryEnabled: true
```

PITR costs approximately $0.20 per GB-month. The learned phrases table will be small (< 1 MB) for a typical deployment. The cost is negligible and the table is a source of truth for accumulated user behaviour — it cannot be rebuilt from code if accidentally deleted. PITR is enabled by default to prevent an irreversible data loss event.

---

## 15. Scene-first dispatch in /execute

`_route_execute` tries scene resolution before device intent parsing. The alternative (try device first, fall back to scene) would require the device resolver to fail before the scene resolver runs, adding latency on every scene command. Scene commands are also semantically different from device commands — "starting work" should never be parsed as an intent targeting a single device. The scene resolver running first is both faster and semantically cleaner. Confidence gating (`SCENE_CONFIDENCE_THRESHOLD`, default `0.4`, `0.70` recommended in production — see decision 9) prevents spurious scene matches on device commands whose vocabulary doesn't overlap with any scene's sample phrases.

---

## 16. SAM --resolve-s3 instead of a named S3 bucket

The deployment pipeline uses `sam deploy --resolve-s3`, which causes SAM to create and manage an artifact bucket automatically. A named bucket (defined in template.yaml or pre-created) would require additional IAM permissions to create and a bucket name that is globally unique. `--resolve-s3` produces a bucket named `aws-sam-cli-managed-default-samclisourcebucket-<hash>` that is reused across deployments in the same region/account, requires no extra IAM statements, and is managed entirely by SAM. The trade-off is that the bucket is not in the CloudFormation stack and will persist after stack deletion; this is acceptable since it contains only deployment artifacts.

---

## 17. IP addresses hardcoded in DEVICE_CATALOG

Kasa device IPs are stored directly in `src/device_resolver.py`. The alternatives — environment variables, DynamoDB, SSM Parameter Store — all add complexity and latency without meaningful benefit at this scale. DHCP reservation should be used at the router level to keep device IPs stable. A future version could read IPs from SSM at cold-start if dynamic assignment becomes a requirement.

---

## 18. GitHub OIDC role assumption over long-lived access keys

**Chosen**: `aws-actions/configure-aws-credentials` with `role-to-assume` + `permissions: id-token: write`
**Rejected**: `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` stored as GitHub Secrets

Long-lived IAM access keys are a persistent credential that can be exfiltrated from GitHub Secrets, rotated inconsistently, or left active after an engineer leaves the team. OIDC eliminates the stored secret entirely: GitHub's OIDC provider issues a signed JWT scoped to the specific repository and workflow run; AWS STS exchanges it for temporary credentials (valid ≤ 1 hour) via `sts:AssumeRoleWithWebIdentity`. The credentials are never stored anywhere and expire automatically.

**How the token exchange works:**

```
GitHub runner                GitHub OIDC           AWS STS
     │                       Provider                 │
     │──── request JWT ──────────►│                   │
     │◄─── signed JWT ────────────│                   │
     │                            │                   │
     │──── AssumeRoleWithWebIdentity(JWT) ───────────►│
     │◄─── temporary credentials (≤1h) ───────────────│
```

**Trust policy constraints:**

The IAM role trust policy restricts assumption to tokens where:
- `aud` = `sts.amazonaws.com` — the audience the `configure-aws-credentials` action always sets; rejects tokens from any other AWS action or third-party tool.
- `sub` matches `repo:rajatarun/DeviceWeave:*` — tokens from other repositories cannot assume this role even if they reach the same AWS account.

The `StringLike` wildcard on `sub` allows all branches and `workflow_dispatch` events. Restricting to `ref:refs/heads/main` would block the `workflow_dispatch` stage selector and manual re-deployments from feature branches — not appropriate for a staging/dev workflow.

**`role-session-name` set to `GitHubActions-DeviceWeave-<run_id>`**

This tags every `AssumeRoleWithWebIdentity` call with a unique, traceable identifier in CloudTrail, making it straightforward to correlate a specific deployment to the AWS API calls it made.

**One GitHub Secret retained**: `AWS_REGION`. This is not sensitive but is kept as a Secret (rather than a hardcoded value or repository variable) to allow per-environment overrides without modifying the workflow file. If the account always deploys to one region this could be promoted to a repository variable.

**IAM role ARN is hardcoded in the workflow env block**, not a secret. The role ARN contains the account ID (`239571291755`) which is not secret — it is visible in any CloudFormation stack output or AWS Console URL. Hardcoding it makes the trust relationship explicit and auditable directly in the workflow file.

---

## 19. LLM as sole semantic interpreter for policy authoring

**Chosen**: natural language → LLM → Policy DSL JSON → deterministic validation → storage
**Rejected**: regex extraction, keyword mapping, heuristic parsing

Rule authoring is a semantic task: "cold" needs to map to `temperature < 65`, "at night" to `time_hour >= 22`, "nobody home" to `is_home == false`. These mappings are not enumerable — they require understanding natural language context. Regex and keyword approaches require maintaining a handcrafted mapping table for every possible phrasing and fail silently on novel input.

The LLM acts as a **compiler**, not a classifier. It receives a structured system prompt that enumerates every allowed field, every allowed value, and every rejection trigger explicitly. It must output valid Policy DSL JSON or an explicit `{"rejected": true, ...}` object — there is no middle ground. Guessing is prohibited by the prompt.

The deterministic validation layer runs after the LLM and enforces the schema independently. The LLM provides the semantic mapping; the validator provides the safety net. Neither step alone is sufficient — together they guarantee that stored rules are both semantically correct and structurally valid.

---

## 20. Confidence threshold at 0.85 for policy compilation

**Chosen**: reject rules with LLM confidence < 0.85
**Rejected**: lower threshold (0.70), no threshold, manual review queue

Policy rules are persistent safety constraints, not one-shot commands. A mis-compiled rule could silently block devices for days or weeks. The execute threshold (0.70 recommended — see decision 9) is appropriate for commands — the system acts and the user immediately sees the result. For policies, the feedback loop is much slower: a wrong rule might not be noticed until the user wonders why their fan is always blocked.

The 0.85 threshold is the same as the learning threshold (decision 9): both represent "the system is confident enough to create a durable record". The LLM is instructed to return an explicit rejection object rather than a low-confidence policy, so the user receives a clear error message explaining what the system could not resolve rather than a silently degraded rule.

The threshold is stored as `POLICY_CONFIDENCE_THRESHOLD` in the environment so it can be tuned per stage without code changes.

---

## 21. Policy Engine positioned between resolution and execution

**Chosen**: check policies after device resolution, before device I/O
**Rejected**: check at intent parse time, check at API entry, post-execution audit

The policy check must know the resolved `device_type` and `action` to evaluate rules — it cannot run before resolution. Checking after execution (audit mode) would allow the blocked action to proceed and creates inconsistency between what the rule says and what happens. Checking at API entry requires parsing intent before the resolver runs, duplicating work.

The chosen position is the only one where both preconditions are met (device + action are known) and the postcondition is guaranteed (no I/O has happened). This gives the Policy Engine complete information with no side effects to undo.

Context (temperature, humidity, time, is_home) is fetched **once per request** at the start of `_handle_device_command` and passed to every enforcement call. This avoids redundant weather API calls on scene commands that evaluate multiple devices.

---

## 22. Context defaults are safe-fail, not safe-block

Every context field has a default used when the data source is unavailable:

| Field | Default | Rationale |
|-------|---------|-----------|
| `temperature` | 70°F | Neutral — neither hot nor cold; prevents spurious temperature policy triggers |
| `humidity` | 50% | Neutral — prevents humidity policy triggers |
| `is_home` | `true` | Prevents accidental "nobody home" lockouts when the presence table is unreachable |

The alternative — default `is_home` to `false` — is safe from a power consumption perspective but creates operational risk: a transient DynamoDB connectivity issue would block all presence-gated devices until the table recovers. "Safe-fail" in a home automation context means the resident can operate their home normally in degraded state, not that devices lock down. Block policies are an opt-in constraint, not a default.

---

## 23. Block > Modify > Allow verdict priority

When multiple policies match the same `(device_type, action, context)` triple, the first BLOCK wins over any MODIFY, which wins over any ALLOW:

```
BLOCK  — safety constraint, highest priority
MODIFY — preference, applies only when no BLOCK matches
ALLOW  — explicit permit, informational in this version (no override semantics)
```

The rationale for BLOCK > MODIFY: if the user said "never run fan when cold" and also "run fan at low speed when cool", the temperature condition that triggers both is ambiguous about intent. Conservatively, BLOCK wins — the user can delete the block rule if the modify rule is intended to supersede it.

ALLOW has no override semantics in the current implementation. An ALLOW policy does not supersede a BLOCK. This is intentional for the v1 safety model: granting an explicit override requires an explicit rule deletion, not an implicit order-of-operations. A future version could add a `priority` field to policies if use cases for allow-overrides-block emerge.

---

## 24. TTL-based policy cache, not event-driven invalidation

**Chosen**: module-level dict with 60-second TTL, reloaded from DynamoDB on expiry
**Rejected**: SNS/EventBridge notification on policy write, ElastiCache, no cache

Policies are safety constraints that should take effect quickly but do not need sub-second propagation. A 60-second window between authoring a rule and its enforcement is acceptable for home automation — users are not authoring rules and immediately testing edge cases in a tight loop. The TTL avoids a DynamoDB read on every `/execute` call (which would add 5–15 ms latency and a DynamoDB read unit cost).

Event-driven invalidation (SNS on write → Lambda subscribes → cache cleared) would add infrastructure complexity (SNS topic, subscription, IAM) for a marginal improvement in propagation time. The PolicyAuthoringFunction and DeviceWeaveFunction are separate Lambda functions; there is no shared memory to invalidate. A cross-function cache coherence mechanism would be more complex than the problem warrants.

`invalidate()` is included for same-container cache busting (e.g. if a future change merges the two Lambdas or in local testing), but cross-container propagation relies entirely on TTL. The 10-second retry TTL on load failure ensures rapid recovery without hammering DynamoDB during an outage.

---

## 25. Separate PresenceTable for home-occupancy state

**Chosen**: `deviceweave-presence-{stage}` DynamoDB table, single item `pk=home_state`
**Rejected**: environment variable, SSM Parameter Store, item in PolicyTable, item in LearningTable

Presence state changes at runtime (user leaves, arrives) and must be writable via the API. An environment variable requires a Lambda redeploy on every change — unacceptable. SSM Parameter Store requires a separate IAM permission and `ssm:GetParameter` API call, adding latency. Storing the state as a special item in PolicyTable (e.g. `rule_id=__system_state__`) is a schema violation — the table's PK is a UUID and the sort key is a version number; a synthetic key breaks both the schema and query patterns.

A single-key DynamoDB table (`pk` String only) is the simplest correct solution: one read per `/execute` call at 1 read unit, one write per presence update, and a clear data model that is easy to reason about and extend (e.g. per-room presence in the future).

---

## 26. DynamoDB device registry replaces static DEVICE_CATALOG

**Chosen**: `DEVICE_REGISTRY_TABLE` DynamoDB table, populated by `POST /ingest`
**Rejected**: static `DEVICE_CATALOG` dict in `src/device_resolver.py`

A static catalog requires a code redeploy whenever a device is added, renamed, or removed. In a household with multiple provider accounts (Kasa, Govee, SwitchBot) devices change frequently enough that a deployment-gated update cycle is unacceptable. Storing the catalog in DynamoDB decouples device management from code releases: `POST /ingest` discovers all provider devices and writes them to the table; the execution path reads the table at runtime.

`DeviceRegistryError` is raised (not silently defaulted) when `DEVICE_REGISTRY_TABLE` is unset or the table is empty. Silent fallback to a static catalog would mask misconfiguration and produce confusing "device not found" errors in production. A hard failure during initialization makes misconfiguration immediately visible.

---

## 27. Scene soft-delete via DynamoDB tombstone

**Chosen**: `DELETE /scenes/{id}` writes a `deleted: true` flag to `SceneTable`; `get_active_scenes()` filters tombstoned scenes at read time
**Rejected**: hard-delete (remove item from DynamoDB), in-memory-only delete, file-based catalog with Git

Hard-deleting a scene item is simpler but creates a gap: if a `POST /execute` in flight resolved the scene before the delete completed, there is no record that the scene ever existed. The tombstone approach preserves history and makes the delete idempotent — a second `DELETE` on the same scene ID is a no-op rather than a 404.

The tombstone is stored in the same `SceneTable` item as the scene definition (`"deleted": True`), so no additional table or GSI is needed. `get_active_scenes()` applies a client-side filter which is efficient at household scale (< 100 scenes).

---

## 28. cloud_cover_pct and is_overcast added to policy context

**Chosen**: Open-Meteo `cloudcover` field mapped to `cloud_cover_pct` (integer 0–100) and `is_overcast` (bool, threshold > 70 %)
**Rejected**: omitting cloud cover, using a paid weather API, binary sunny/cloudy from a different source

Cloud cover is the primary environmental signal for lighting automation ("don't dim lights when it's overcast"). Open-Meteo already provided `temperature` and `humidity`; adding `cloudcover` from the same API call incurs zero extra latency and no additional API quota. The raw percentage is exposed as `cloud_cover_pct` for fine-grained rules; `is_overcast` is a pre-computed boolean for the common binary case, keeping policy DSL rules readable (`is_overcast: true`).

The 70 % threshold for `is_overcast` is a meteorological convention for "mostly cloudy" (≥ 7 oktas of 8). Rules that need custom thresholds can use `cloud_cover_pct` directly.

---

## 29. Bedrock Converse API for conversational agent

**Chosen**: `client.converse()` with `toolConfig`, agentic loop capped at 10 rounds, stateless module with caller-managed history
**Rejected**: `client.invoke_model()` with manual JSON construction, LangChain/LlamaIndex, stateful Lambda (Step Functions)

The Bedrock Converse API provides a unified interface across Claude model versions with native tool-use support (`toolConfig`, `toolResult` message blocks). Using `invoke_model()` would require manual JSON construction for each model family and custom tool-call parsing — significant boilerplate with no benefit.

`bedrock_agent.py` is a pure function (`run_agent(user_message, history, ...) → (reply, updated_history)`). History is loaded and saved by the caller (`conversation_store.py`). This separation means the same agent can be driven by the HTTP path and the SMS handler without any shared state in Lambda memory — each invocation is independent, which is essential for correct behavior under Lambda's concurrent execution model.

The 10-round cap prevents runaway tool-call loops. In practice, two to four rounds suffice for any realistic home-automation command (list → execute → confirm). Exceeding 10 rounds indicates a model confusion loop; hard-stopping and returning an error is safer than retrying indefinitely.

---

## 30. canonical_phrase required for conversational context-aware learning

**Chosen**: `canonical_phrase` required parameter on `execute_device_command` and `execute_scene` tools; the agent derives it from conversation context
**Rejected**: learning verbatim user text, not learning from conversational executions at all

The learning store (cosine similarity on sample phrases) is only useful if stored phrases are self-contained and unambiguous. Storing verbatim user text like "kitchen too" or "dim it a bit" produces a corpus that will never match future inputs correctly — "kitchen too" has no meaning without the preceding turn.

`canonical_phrase` is defined in the tool schema as "a short, self-contained phrase that fully describes the resolved intent using conversation context — no pronouns or relative references". The agent must populate it on every execution. The system prompt includes concrete examples:

```
user says "kitchen too" after turning on living room light
  → canonical_phrase: "turn on kitchen island light"
user says "dim it a bit"
  → canonical_phrase: "dim living room ceiling light to 50 percent"
```

The phrase is passed to both `save_learned_phrase()` and `graph_engine.record_event()`. This ensures that even terse follow-ups produce high-quality learning entries that improve future intent resolution.

---

## 31. AWS End User Messaging over Amazon Pinpoint

**Chosen**: `boto3.client("pinpoint-sms-voice-v2")`, `send_text_message()`, SNS TopicPolicy principal `sms-voice.amazonaws.com`
**Rejected**: legacy `pinpoint` boto3 client, Twilio, other SMS gateway

Amazon Pinpoint SMS was decommissioned and replaced by AWS End User Messaging. The new service uses the `pinpoint-sms-voice-v2` boto3 client and `send_text_message()` API — the old `pinpoint` client's `send_messages()` API no longer works for SMS. There is no migration shim.

The SNS topic policy principal is `sms-voice.amazonaws.com` (not `sns.amazonaws.com`): End User Messaging publishes inbound SMS events to SNS under its own service principal. Using the wrong principal causes all inbound SMS to be silently dropped. IAM action is `sms-voice:SendTextMessage` (not `mobiletargeting:SendMessages`). Both changes are non-obvious because legacy Pinpoint documentation still surfaces prominently in search results.

---

## 32. Phone number as session_id for SMS conversations

**Chosen**: `session_id = f"sms:{sender}"` where `sender` is the E.164 `originationNumber` from the EUM SNS event
**Rejected**: per-message ephemeral sessions, a separate SMS session table, user-initiated session tokens

The SMS channel has no concept of a session token — the only persistent identifier available without user enrollment is the sender's phone number. Using it as the session key means every message from the same phone automatically continues the previous conversation, which is the expected UX for any SMS-based assistant.

The `sms:` prefix namespaces SMS sessions separately from HTTP sessions in `ConversationTable` — a collision between a phone number and a UUID is impossible by construction. The same DynamoDB table is reused (no second table) because the data model and TTL semantics are identical: both paths store a list of Converse API message dicts with a 24-hour TTL.

Reset keywords (`reset`, `new`, `clear`, `restart`, `start over`) clear the session without invoking the agent. This is a safety valve: if the conversation history becomes stale or the agent gets confused, the user can recover with a single word.

---

## 33. Rule-set analysis by interval arithmetic, cross-checked against Z3

**Chosen**: `policy_authoring/rule_set_checker.py` — each rule's matching contexts as a union of boxes, exact satisfiability / overlap / coverage by interval arithmetic, run on every `POST /policies/author` against the device's active policies; `scripts/policy_rules_smt.py` encodes the same questions for Z3 and the tests require identical answers on random rule sets
**Rejected**: per-rule validation only; Z3 as a Lambda dependency

The validator checks one rule at a time, so it accepts a rule that contradicts itself (`temperature > 80 AND temperature < 60`), a modify that a block always overrides, two modifiers that disagree about the same moment, and a rule set in which a stated safety property has a gap. Each goes live and silently does something other than what was asked.

Every DSL condition constrains one field and a rule ANDs its conditions, so a rule matches a union of axis-aligned boxes (`!=` splits one field in two) and the questions are decidable exactly with intervals — integer `time_hour` included, so `22 < time_hour < 23` is unsatisfiable. That keeps the Lambda free of a ~30 MB native wheel. Z3 is kept as an independent encoding rather than dropped: 200 random rule sets must produce the same findings from both, and a mutation that makes `>` inclusive fails both that test and a second one replaying every witness through the real `policy_engine.evaluator`. When the DSL grows a cross-field condition, the box argument stops holding and the Z3 encoding is the one to keep.

On authoring: an `unsatisfiable` rule, or an `invariant_violated` against `POLICY_INVARIANTS` (optional JSON list of `{name, device_type, conditions}` meaning "must be blocked from switching on there"), is a 422 with `rejection_stage: "rule_set_analysis"` and is not stored. Conflicts, shadowing and redundancy are warnings returned with the stored policy under `analysis`, each with a witness context. The analysis assumes every context field is present; a missing field can only stop a block firing, which the evaluator already treats as no match.

`scripts/policy_compile_bench.py` measures the compiler that feeds all of this: 380 plain-English rules with gold policies (single conditions, conjunctions, modifiers, 20 that must be refused, 20 self-contradictions). It scores by *meaning* — two rules are equal when they match exactly the same contexts — and reports the silent error rate: rules the validator accepted that mean something else.

## 34. Compile fidelity measured on AutoTap Study 1, referenced rather than vendored

**Chosen**: `scripts/compile_fidelity.py` reads AutoTap's Study 1 spreadsheet (Zhang, He et al., ICSE 2019, <https://ieeexplore.ieee.org/abstract/document/8811900>, <https://github.com/zlfben/autotap>) from a local, git-ignored path at run time. It scores each statement through the real compiler, validator and rule-set checker, and compares against position-keyed labels in `benchmarks/autotap/labels.json`.
**Rejected**: copying the data or paraphrased rules into the repository; a spreadsheet library dependency; a live model call in tests.

The generated benchmark (decision 33) only contains phrasings the compiler prompt was written for. Study 1 is 690 statements people wrote about their own homes, so it shows what the DSL is *asked* for. Most of them are obligations ("should always") or involve devices the DSL does not know about (`docs/autotap-gap-analysis.md`).

The AutoTap repository is GPL-3.0, and no separate data licence was found. So:
- the file is fetched (pinned commit, sha256-checked) into a cache that `.gitignore` excludes twice;
- labels are keyed by sheet, row and statement slot, and refuse free-text fields that repeat the statement;
- reports leave the text out unless asked;
- a test fails if any spreadsheet is ever tracked.

The `.xlsx` is read with the stdlib zip/XML parser, because the format is three XML parts. The compiler mode is explicit: `--responses` scores saved outputs offline, and `--live` is an opt-in paid call that sends the statements to the configured provider. Unlabelled statements are reported as `unlabeled`, never as failures, so the harness is useful before anyone has labelled anything.

## 35. Enforceability measured on hand-coded properties, not on compiled policies

**Chosen**: two independent coders formalise each AutoTap Study 1 statement with a DSL-independent codebook built on AutoTap's own templates (`docs/paper/codebook.md`). `scripts/property_coding.py` turns each code into a small abstract plant and classifies it with `controllability.supcon_forcing`, under five enforcement architectures and four assumption sets.
**Rejected**: computing enforceability shares from compiled policies (only the few statements the five-device DSL can express ever get that far); keyword tags as ground truth; an LLM standing in for either human coder.

A share computed from compiled policies measures the compiler and the DSL, not what people ask for. The coded property is the only input independent of both.

The abstraction is deliberate and stated in the codebook:
- one boolean for the target and one for the condition;
- a 0–2 duration counter;
- one step of grace after an uncontrollable change ("reaction"), which can be switched off.

Outcomes distinguish *exact* from *preemptive* (forcing before anything happened) and *over-restrictive* (safe only by refusing what the property allows). A refusal-only guard is never credited with a property it can only satisfy by also refusing commands the user allowed. The paper draft holds placeholders only, and a test fails if one names a key the analysis does not produce.

---

## 36. Eero presence polls the unofficial cloud API into the existing presence item

**Chosen**: a Lambda outside the VPC, invoked by EventBridge Scheduler (default `rate(5 minutes)`), calling the same unofficial eero cloud API as `eero-client` and the Home Assistant eero integration. Tracked devices (MAC or name → person) live in a small on-demand DynamoDB table. The poller writes `pk=home_state` through `presence_store`, which `GET /presence` and the policy engine already read. `is_home` stays the occupancy flag; zone, node, signal, last_seen, and freshness are extra attributes on that item. The session token is written once by `scripts/eero_login.py` into Secrets Manager (`deviceweave/eero-session` by default). The function's IAM allows `secretsmanager:GetSecretValue` on that secret only.

**Rejected**: new presence hardware; running the poll inside the VPC ingestion Lambda (the private subnets need NAT to reach eero, and this feature stays pay-per-request); a second occupancy table; marking everyone away when eero returns 401 or the client list cannot be fetched; putting the session token or device MACs in the template or the repo; a SAM `Schedule` event (EventBridge rule) instead of Scheduler, because the schedule has to be a deploy parameter and Scheduler is the resource that takes a schedule expression directly.

Departure waits for `EeroAwayMissThreshold` consecutive misses (default 3) of every device mapped to that person. Arrival is immediate. Auth failure, transport failure, a blank secret, and an empty tracked-device table call `apply_stale`: the last `is_home` is kept, `freshness` becomes `stale`, and a missing record defaults to home. That matches decision 22. The secret name and schedule are passed as non-empty workflow overrides; `AllowedPattern` and `MinLength` reject a blank secret name, which is the empty-parameter failure that broke earlier deploys.

The API is unofficial and may change. The client pins the paths those libraries use (`/login`, `/login/verify` with cookie `s`, `/account`, `/networks/{id}/devices`) and tests them with mocked HTTP so CI never calls eero.

Amazon sign-in is not supported: `POST /login` returns HTTP 401 and `error.login.unknown`. The login CLI is for a separate standard account (email or phone, one-time code) that the owner invites as a network admin. A login 401 is reported with those steps. A later session 401 (`error.session.invalid`, `error.session.refresh`) stays on the fail-safe path and is not described as an Amazon-login problem.

---

## 37. Room calibration stores node and coarse signal, and will not guess a room

**Chosen**: a second Lambda, also outside the VPC, is the only writer of an on-demand fingerprint table (encrypted at rest, point-in-time recovery off). `calibrate_room(person, room, samples=5)` and `POST /presence/rooms/calibrate` queue about a minute of eero reads (every 12 seconds, at most 8) and return before that minute finishes, because API Gateway will not hold the request open. Calling it again for the same person and room replaces the samples. `list_room_fingerprints` and `delete_room_fingerprint(room)` read and delete. The HTTP routes require the same bearer token as `/mcp`. The poller may read the table and may not write it. The MCP function may invoke the calibration function and may not write the table.

The cloud payload does not include a live RSSI or a signal-sample timestamp. Samples store the node plus a coarse bucket from `connectivity.score_bars` (1–5), else `connectivity.signal` / `signal_avg` (quality word or a negative dBm number). A fractional `score` is not mapped. Classification is nearest bucket among rooms that have a sample on the same node. A tie, an unseen node, or confidence below 0.5 yields `room=unknown` and leaves `zone` as the node. The displayed room changes only after two agreeing polls. Smoothing state sits on the current presence item. Live polls are not appended to the fingerprints.

Device ids on fingerprints and presence are HMAC-SHA256 under `deviceweave/eero-device-hmac`, not the eero session token. The tracked-device table still stores the MAC the poller matches on. Logs for this path are counts and room labels.

**Rejected**: blocking the HTTP request for the whole minute; deriving the HMAC from the rotating eero session; storing raw MACs on the fingerprint or presence items; keeping a history of live readings; letting the poller write fingerprints; guessing a room when two labels are equally near; inventing an RSSI field the cloud API does not return.

---

## 38. Labeled room check-ins live in DynamoDB

**Chosen**: a pay-per-request check-in table (`pk` person, `sk` ask-time plus a random suffix) with the default AWS owned encryption key and no TTL. The calibration Lambda, already outside the VPC, is the only writer. `record_room_checkin` reads eero once and stores the label with the snapshot. `capture_room_reading` stores the snapshot without a label and returns `checkin_id`; `label_room_checkin` sets the room later and does not read eero again. Hour and weekday are `asked_at` in America/Chicago (Monday is 0). The eero fields are the live cloud snapshot, not a reconstruction of a past `asked_at`. A failed eero read still stores the label with `freshness=stale` and omits `is_home`. Device ids are the fingerprint HMAC. Floats become `Decimal` at `put_item`, and JSON responses convert them back. The MCP function invokes the calibration function. The poller has no access to the table. HTTP routes use the MCP bearer token. There is no new secret and no new deploy parameter, so the changeset stays on `CAPABILITY_IAM`.

**Rejected**: writing the training rows on the assistant's machine; a new Lambda; a GSI just to find a check-in by id (the id carries the person and the sort key); a TTL that would drop the week before training; `SSEEnabled: true` (that is an AWS managed KMS key, not the default owned key); an explicit IAM name; the calibration room allowlist (a check-in room may be `Not home` or any short string); storing raw MACs; treating a failed eero read as away.

---

## 39. A fixed local window can name the room, and classification matches the eero the fingerprint shows

**Chosen**: schedule rules live in their own on-demand table (default AWS owned key, no TTL, no custom IAM name). `set_room_schedule_rule(person, room, start, end, timezone=America/Chicago)` stores one window. `end` earlier than `start` wraps midnight. The calibration function is the only writer. The poller may read. While the person is home inside the window, the poller sets `room` to the rule, `room_confidence` to 1, and `room_source` to `schedule_rule`, without waiting for a second sample. It also puts one check-in per local hour (`source=schedule_rule`) and cannot list or delete check-ins. The hour marker sits on the presence person so a 5-minute poll does not write twelve rows.

Classification was returning `unknown` / `0.0` for a reading that showed the same eero and the same dBm as a fingerprint. Three comparisons caused that. Node equality was exact, so `Family Room` and `Family Room eero` never met. `score_bars` (and a float `score` of `1.0`) outranked the dBm string the fingerprint displays, and an unknown room is stored with confidence 0. Fingerprints were selected only by device HMAC, so a name-tracked phone whose MAC changed dropped its rooms. A sample now matches on a case-insensitive node with a trailing `eero` removed, using either the location or the display name. A dBm signal is bucketed before `score_bars`. A fingerprint applies when the person matches or the HMAC matches.

**Rejected**: hard-coding Raja's night window in the template (MCP has to be able to change it); letting the poller delete check-ins; treating `score` `1.0` as bar 1; keeping HMAC as the only key for a person who is tracked by name.

---

## 40. Per-tool scopes, 60-second step-up, and the door gated where it opens

**Chosen**: every MCP tool needs one CipherWeave token scope — `read` (readOnlyHint), `execute` (state changes, and the `execute` tool itself), or `destructive` (destructiveHint, plus `ingest`). The decision is `cipherweave_verify.ToolAuthorizer`, vendored, so the other Weaves can adopt the same code. A tool with no declared policy is treated as destructive. `tools/list` shows only what the token can call. Destructive tools also need **step-up**: `iat` at most 60 s ago, and the token's `jti` spent in `deviceweave-token-jti-<stage>` by a conditional put (PutItem only; TTL just past the token's own expiry), so a fresh token works once. No ledger, or a ledger error, refuses the call. The legacy static token carries `LegacyTokenScopes` (default all three) but can never step up.

Unlock is not a tool: `execute` takes natural language, and only the resolver knows it means the front door. So the gate is in `execution_planner._execute_one`, the one place every path converges — MCP, the REST `/execute` route, SMS, both conversational agents. `unlock`, `open`, and `toggle` on a device that can do either need the same token a destructive tool would; one request spends one `jti` however many doors it opens. With no MCP principal in context (`access_control`, a context variable that follows `asyncio.run` into gathered tasks) the step is refused. `lock` and `close` are never gated: the safe direction must not need a fresh token.

A refusal is HTTP 403 with `WWW-Authenticate: Bearer error="insufficient_scope", scope=…` or `error="insufficient_user_authentication", max_age="60"` (RFC 6750, RFC 9470). Every `tools/call` and every physical-access step logs one JSON line with the token's `sub` and `jti`. `ToolScopeMode=audit` (repository variable `TOOL_SCOPE_MODE`) logs scope failures instead of refusing them, for the window while existing clients are granted scopes; step-up stays enforced in both modes.

**Rejected**: requiring `destructive` + step-up for all of `execute` (every lamp would need a 60-second token); gating unlock in the MCP handler by guessing from the command text; letting the REST route keep unlocking without a token because it has no authorizer (it has none — that is the bigger hole, and closing the door path there is the part that cannot wait for an API authorizer); an in-process jti set (one per Lambda instance, so a replay to a second instance passes); treating scopes as a ladder.

---

## 41. The REST API takes the /mcp credential, and a scope per route

**Chosen**: `rest_auth.check` runs first in the three functions that serve REST routes (`app`, `policy_authoring.handler`, `ingestion_handler`). Every route but `GET /health` needs what `/mcp` needs — a CipherWeave token for `deviceweave` (Bearer, or DPoP with its proof) or the legacy static token while `AcceptLegacyToken` is true — decided by the same `BearerAuth`, so the two doors cannot disagree. Each route then needs a scope: `read` for GETs, `execute` for writes, `destructive` (no step-up; not delegable) for deleting a device, scene or policy and for an ingest. An unlisted write route needs `execute`, never less. For `/execute` the principal is bound into `access_control`, so the REST route unlocks exactly when the MCP tool would — previously it could never unlock (no principal), and now it can only with the same fresh token. A non-HTTP invocation (the scheduled ingest) is IAM-authorized and is not checked.

**Why now**: ADR 40 recorded the REST API as "the bigger hole" and closed only the door path. The rest stayed open: anyone with the API URL — which is in the stack outputs, the OpenAPI document and every client's configuration — could switch devices, read who is home from `/presence`, delete devices, and rewrite the automation policies that `policy_engine` enforces.

**Rollout**: `RestAuthMode` (repository variable `REST_AUTH_MODE`) is `enforce` by default. `audit` lets read and execute routes through with a log line, for the window while existing REST clients are given a token; destructive routes and the delegation rule are enforced in both modes.

**Rejected**: an API Gateway authorizer (a second implementation of the token check, and the SIWE authorizer answers a different question — a human wallet session, not an agent token); leaving `GET /presence` public (it is the most sensitive read on the API: whether the house is empty); step-up on REST deletes (the REST functions have no jti ledger, and a deleted device comes back on the next ingest).

---

## 42. Stable rule winner, MODIFY range clamp, fail closed when the check raises

**Chosen**: When two or more rules of the winning type match the same device class, action, and context, the winner is the lowest `rule_id` string, then the reason, then a canonical rendering of the params. BLOCK still beats MODIFY, which still beats the default ALLOW. The verdict was already independent of list order; the rule id, the reason, and the MODIFY params now are too. `rule_set_checker` and the Z3 cross-check use the same order, so a shadow or modify-conflict witness names the rule the evaluator applies.

When a MODIFY verdict is applied to a device, known numeric params are clamped to that device's `capability_ranges` before actuation. Brightness defaults to 0–100 when the device does not declare a range. Duration, and any other numeric param, is clamped only when the device declares a range for it. `execution_planner._execute_one` applies the same clamp immediately before the adapter call, so a caller cannot forward an unclamped value.

The four conversational-agent tool handlers catch a policy-check exception and return an error without calling the adapter: `bedrock_agent._tool_execute_device_command`, `bedrock_agent._tool_execute_scene`, `gemini_agent.execute_device_command`, and `gemini_agent.execute_scene`.

**Rejected**: keeping list order or "newest first" (the loader's scan order is not a creation order, and shuffling the same rules changed the reported rule); merging MODIFY params; inventing a duration bound the device did not declare; continuing with no verdict when the policy check raises.

**Not covered**: `turn_off` and `get_status` stay exempt from BLOCK. Scope is still the device class only. No adapter type maps to `heater` or `ac`.

---

## 43. Check-in Naive Bayes room model beside fingerprints

**Chosen**: Labeled room check-ins (including schedule-rule rows) train a dependency-free multinomial Naive Bayes classifier over node, signal bucket, node+bucket, local hour, and weekday. The model is stored on the room fingerprint table as ``pk=classifier#<person>``, ``sk=model#v1``. The presence poller loads it from the same scan, applies it before fingerprint distance matching, and falls back to fingerprints when the model is missing or returns ``unknown``. Overnight schedule rules still overwrite the room afterward with confidence 1.0. Retrain with ``scripts/train_room_classifier.py``.

**Rejected**: sklearn or other heavy ML stacks in Lambda; replacing fingerprints entirely (calibration samples still seed cold start); storing the model only in the check-in table (poller already scans fingerprints, not check-ins); letting a classifier failure change ``is_home`` (failures carry the last room forward).

**Not covered**: Per-person models only; rooms with no check-ins still depend on fingerprints; class imbalance and sparse rooms (e.g. Game Room) need more labels before the model beats ``unknown``.
