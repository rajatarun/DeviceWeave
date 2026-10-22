# CLAUDE.md – DeviceWeave

> Project context for Claude and other LLM-based tools working in this repository.
> The platform-wide engineering standard is `docs/succession.md` in
> [ContextWeave](https://github.com/rajatarun/ContextWeave/blob/main/docs/succession.md).
> This file is the DeviceWeave-specific part of it.

---

## Project identity

**Name**: DeviceWeave — an AI execution substrate for the physical world
**Owner**: Tarun Raja (rajatarun)
**Account**: AWS `239571291755`, `us-east-1`, SAM via GitHub Actions OIDC
**One line**: LLMs generate intent, DeviceWeave enforces policy, hardware executes.

This code switches real devices — lights, fans, locks — in a home. **There is
no undo.** Every design choice below follows from that.

`DECISIONS.md` (41 numbered decisions) is the authoritative record of why;
`README.md` documents the API, the policy DSL and deployment.

---

## The execution path

Scene resolver → regex intent parser → device resolver (TF cosine + learned
phrases) → LLM resolver (tier 2, only below threshold) → **Policy Engine**
(BLOCK/MODIFY/ALLOW) → safety layer (capability, params, idempotency) →
provider registry (Kasa, Govee, SwitchBot) → learning write.

Every caller converges on `execution_planner._execute_one`: MCP, REST
`/execute`, SMS, and both conversational agents. That is the one place where a
gate covers every path.

---

## Invariants — do not break these

1. **No model call can actuate hardware directly** (Safety model; #3, #19,
   #21). LLMs are bounded to tier-2 resolution and policy compilation. The
   deterministic Policy Engine and safety layer always sit between a model and
   the device.
2. **The intent parser is deterministic** (#3). It has a small, closed action
   set. Do not put an LLM on the critical path.
3. **Policy authoring = LLM compiler + deterministic validator + rule-set
   analysis** (#19, #20, #33). The model must emit valid DSL or
   `{"rejected": true}`. The validator enforces the schema independently.
   `rule_set_checker.py` rejects unsatisfiable rules and invariant violations
   (422, `rule_set_analysis`) and warns on conflicts and shadowing, with a
   witness. Its interval arithmetic is cross-checked against Z3 on random rule
   sets; keep the two agreeing.
4. **Verdict priority is Block > Modify > Allow** (#23). ALLOW never overrides
   a BLOCK.
5. **Context defaults are safe-fail, not safe-block** (#22). An unreachable
   sensor or presence table yields neutral values (70°F, 50%,
   `is_home=true`), so a transient outage does not lock the resident out.
6. **Confidence gates are three separate gates** (#9):
   - `SCENE_`, `DEVICE_` and `LLM_CONFIDENCE_THRESHOLD`, each falling back to
     `CONFIDENCE_THRESHOLD`.
   - `0.4` ships as a *testing* default; `0.70` is the production
     recommendation.
   - The LLM gate reads the model's *self-reported* confidence, the weakest
     signal of the three; hold it to the highest bar.
   - Learning writes only at ≥ 0.85, a separate threshold.
7. **Physical access is gated where it happens** (#40):
   - Unlock is not a tool. `execute` takes natural language and only the
     resolver knows it means the door, so `unlock`/`open`/`toggle` on such a
     device need a step-up token, enforced in `_execute_one`.
   - Step-up means `iat` ≤ 60 s ago, plus a `jti` spent once in a DynamoDB
     ledger (not in-process, which would allow a replay to another instance).
   - Destructive tools need step-up even in audit mode.
8. **Every REST route but `GET /health` is authenticated** (#41).
   `rest_auth.check` uses the same `BearerAuth` as `/mcp`, so the two doors
   cannot disagree. `GET /presence` reveals whether the house is empty and is
   never public.
9. **A tool with no declared policy is treated as destructive** (#40).
   `tools/list` shows only what the token can call.

---

## Shared code — never edit in place

| Vendored | From | Checksum / pin |
|---|---|---|
| `src/cipherweave_verify/` | CipherWeave | `requirements-cipherweave.txt`, `VENDORED.json`; upgrade with the sync script |
| `src/modelweave_roles.py` | ModelWeave | `MODELWEAVE_VENDORED.json` |
| `src/bedrock_cost.py` | ModelWeave | `BEDROCK_COST_VENDORED.json` |
| `src/mcp_era.py` | identical in ContextWeave and ModelWeave | dual-era MCP (2026-07-28 stateless + handshake revisions) |

Spans go to the shared `OBSERVATORY_METRICS` table. `tests/test_shared_table_contract.py`
holds the item shape to mcp-observatory's contract.

---

## Working here

```bash
python -m pip install -r requirements-dev.txt
python -m pytest            # what CI runs (plus pip-audit and gitleaks)
```

- Deploy: `.github/workflows/deploy.yml`. It resolves the VPC, the Memgraph
  instance, the observatory table and the CipherWeave issuer from other stacks.
  An output that is absent there must be handled, not passed through as an
  empty string or the word `None`.
- Rollout modes: `ToolScopeMode` / `TOOL_SCOPE_MODE` and `RestAuthMode` /
  `REST_AUTH_MODE` (`audit` while clients get scopes). `AcceptLegacyToken`
  stays true during migration. Step-up and destructive routes are enforced in
  every mode.
- Policy compiler quality: `scripts/policy_compile_bench.py`. It scores by
  meaning (do two rules match the same contexts?) and reports the *silent error
  rate*, meaning rules the validator accepted that mean something else.

## When adding a decision

Append a numbered section to `DECISIONS.md`: Chosen, Rejected, and why. Ship
tests that fail when the guarantee is removed, and say what the change does
*not* cover yet.
