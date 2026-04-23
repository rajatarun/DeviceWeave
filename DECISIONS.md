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

## 9. Learning threshold at 0.85, execute threshold at 0.70

**Execute threshold (0.70)**: below this the match is too uncertain to act on. The system returns a 422 with a hint to use `/learn`.

**Learning threshold (0.85)**: a phrase is only persisted as a training example if the system was already confident. Persisting low-confidence matches would reinforce incorrect associations and degrade future resolution accuracy. The gap between the two thresholds (0.70–0.85) is the "execute but don't learn" zone — the system will act but not treat the phrase as a reliable example.

Both values are configurable via environment variables (`LEARNING_CONFIDENCE_THRESHOLD` in template.yaml) so they can be tuned per stage without code changes.

---

## 10. DynamoDB ConditionalCheckFailedException for idempotent writes

`save_learned_phrase` uses `ConditionExpression="attribute_not_exists(phrase)"` on `put_item`. If the phrase is already stored, DynamoDB raises `ConditionalCheckFailedException` (caught via `botocore.exceptions.ClientError`). The handler then increments `use_count` via `update_item`. This approach avoids a read-before-write and is safe under concurrent Lambda invocations — two containers writing the same phrase simultaneously will have one succeed and one fall through to increment. The alternative (scan for existence first) is not atomic and costs an extra read unit.

---

## 11. Provider registry pattern

**Chosen**: `dict[str, BaseDeviceProvider]` keyed by `device_type` string, built at module load time.
**Rejected**: `if device_type == "SmartPlug": ... elif device_type == "SmartBulb": ...` chains in the execution path.

The if/elif chain is the simplest implementation but scatters protocol knowledge across the codebase and requires modifying the execution planner to add a new protocol. The registry keeps all protocol knowledge inside `providers/`. Adding Matter or Zigbee is three steps: create an adapter, register it in `providers/__init__.py`, add `device_type` to catalog entries. The execution planner, safety layer, and app handler do not change.

---

## 12. boto3 excluded from requirements.txt

`boto3` is pre-installed in the Lambda Python 3.11 runtime. Including it in `requirements.txt` causes SAM to bundle it into the deployment package, adding approximately 10 MB. It is deliberately excluded from `src/requirements.txt`. Developers installing dependencies locally for testing should run `pip install boto3` separately. This is documented in the README and in `learning_store.py`.

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

`_route_execute` tries scene resolution before device intent parsing. The alternative (try device first, fall back to scene) would require the device resolver to fail before the scene resolver runs, adding latency on every scene command. Scene commands are also semantically different from device commands — "starting work" should never be parsed as an intent targeting a single device. The scene resolver running first is both faster and semantically cleaner. Confidence gating (≥ 0.70) prevents spurious scene matches on device commands whose vocabulary doesn't overlap with any scene's sample phrases.

---

## 16. SAM --resolve-s3 instead of a named S3 bucket

The deployment pipeline uses `sam deploy --resolve-s3`, which causes SAM to create and manage an artifact bucket automatically. A named bucket (defined in template.yaml or pre-created) would require additional IAM permissions to create and a bucket name that is globally unique. `--resolve-s3` produces a bucket named `aws-sam-cli-managed-default-samclisourcebucket-<hash>` that is reused across deployments in the same region/account, requires no extra IAM statements, and is managed entirely by SAM. The trade-off is that the bucket is not in the CloudFormation stack and will persist after stack deletion; this is acceptable since it contains only deployment artifacts.

---

## 17. IP addresses hardcoded in DEVICE_CATALOG

Kasa device IPs are stored directly in `src/device_resolver.py`. The alternatives — environment variables, DynamoDB, SSM Parameter Store — all add complexity and latency without meaningful benefit at this scale. DHCP reservation should be used at the router level to keep device IPs stable. A future version could read IPs from SSM at cold-start if dynamic assignment becomes a requirement.
