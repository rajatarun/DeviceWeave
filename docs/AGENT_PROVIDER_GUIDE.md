# Conversational Agent Provider Guide

DeviceWeave now supports two conversational agents for SMS and chat interactions:

1. **Bedrock Converse API** — AWS-native, cross-region inference
2. **Google Gemini Live API** — Low-latency streaming, cost-efficient

## Architecture Overview

### Agent Providers (SMS, Chat Conversations)
- **bedrock_agent.py** — Bedrock Converse API with native tool calling
- **gemini_agent.py** — Google Gemini Live API with streaming responses
- **agent_factory.py** — Provider selection and unified async interface

### LLM Providers (Non-agentic: resolution, policy authoring)
- **llm_provider/** — Pluggable: Bedrock, Gemini, Ollama

These are **independent**. You can use:
- Bedrock agent + Gemini LLM (optimize cost for resolution)
- Gemini agent + Bedrock LLM (ensure consistency)
- Or match them

## Configuration

### Environment Variables

#### AGENT_PROVIDER (agent selection)
```bash
AGENT_PROVIDER=auto      # Default: Bedrock if online, Gemini if offline
AGENT_PROVIDER=bedrock   # Always Bedrock Converse API
AGENT_PROVIDER=gemini    # Always Gemini Live API
```

#### Bedrock Configuration (if using Bedrock agent)
```bash
LLM_MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0  # Model ID
AWS_REGION=us-east-1                                      # Region
```

#### Gemini Configuration (if using Gemini agent or LLM)
```bash
GEMINI_LIVE_MODEL=gemini-3.1-flash-live-preview  # Agent model (default)
# for non-agentic calls:
GEMINI_MODEL=gemini-2.5-flash                           # generateContent model

# API key secret is hardcoded to: gemini/api_key
# Secret format: {"api_key": "YOUR_API_KEY"}
```

#### LLM Provider Configuration (non-agentic calls)
```bash
LLM_PROVIDER=gemini      # Recommended (cheaper)
LLM_PROVIDER=bedrock     # Always use Bedrock
LLM_PROVIDER=auto        # Adaptive (default)
```

## Deployment Scenarios

### 1. Cost-Optimized (Gemini Primary)
```yaml
AGENT_PROVIDER: gemini        # SMS via Gemini Live API
LLM_PROVIDER: gemini          # Device resolution via Gemini
LLM_MODEL_ID: (ignored)       # Not used
```
**Cost**: ~$0.00075 per SMS (vs ~$0.001 with Bedrock)
**Latency**: <50ms streaming responses

### 2. Bedrock-Only (Default/Safe)
```yaml
AGENT_PROVIDER: bedrock       # SMS via Bedrock Converse
LLM_PROVIDER: bedrock         # All calls via Bedrock
LLM_MODEL_ID: claude-haiku... # Bedrock model
```
**Cost**: ~$0.001 per SMS
**Latency**: ~100ms responses
**Requires**: Outbound internet (cross-region inference)

### 3. Hybrid/Adaptive (Recommended for VPC with NAT variance)
```yaml
AGENT_PROVIDER: auto          # Switch based on connectivity
LLM_PROVIDER: auto            # Device resolution falls back gracefully
```
**Behavior**:
- Online (NAT up) → Bedrock (low latency, cross-region)
- Offline (NAT down) → Gemini (via VPN/bastion, API calls)
- Probe cached for 60 seconds to minimize overhead

### 4. Bedrock Agent + Gemini LLM (Best of both)
```yaml
AGENT_PROVIDER: bedrock       # SMS uses Bedrock Converse (required for consistency)
LLM_PROVIDER: gemini          # Device resolution cheaper via Gemini
```
**Cost**: Mid-range (~$0.0008/SMS)
**Benefit**: Consistent agent responses + cheaper background calls

## Setup Instructions

### For Gemini Agent/LLM

1. **Create Gemini API Key**
   ```bash
   # Visit: https://aistudio.google.com/apikey
   # Create key, copy it
   ```

2. **Store in Secrets Manager**
   ```bash
   aws secretsmanager create-secret \
     --name gemini/api_key \
     --secret-string '{"api_key": "YOUR_API_KEY"}'
   ```

3. **Deploy with Gemini**
   
   **Option A: Local SAM deployment**
   ```bash
   sam deploy --parameter-overrides \
     AgentProvider=gemini \
     LLMProvider=gemini
   ```
   
   **Option B: Update existing stack via CloudFormation**
   ```bash
   aws cloudformation update-stack \
     --stack-name deviceweave-prod \
     --use-previous-template \
     --parameters \
       ParameterKey=AgentProvider,ParameterValue=gemini \
       ParameterKey=LLMProvider,ParameterValue=gemini \
       ParameterKey=StageName,UsePreviousValue=true \
       ParameterKey=VpcId,UsePreviousValue=true \
       ParameterKey=LambdaSubnetIds,UsePreviousValue=true
   ```
   
   **Option C: AWS Console**
   - Go to CloudFormation → DeviceWeave stack
   - Update stack → Next
   - Set AgentProvider=gemini, LLMProvider=gemini
   - Review and submit

### For Bedrock Agent (Default)

No additional setup — uses Bedrock cross-region inference profile.

Deploy with:
```bash
sam deploy --parameter-overrides \
  AgentProvider=bedrock
```

## Provider Comparison

| Feature | Bedrock | Gemini |
|---------|---------|--------|
| **Cost** | $0.00080/1K input tokens | $0.0000075/1K input tokens (~100x cheaper) |
| **Latency** | ~100ms | <50ms (streaming) |
| **Tool Calling** | Native (Converse API) | Native (Function Calling) |
| **Requires Internet** | ✓ (cross-region) | ✓ (API calls) |
| **Fallback** | Gemini (if offline) | None (hard failure) |
| **Multi-turn History** | Bedrock format | Gemini format (auto-normalized) |
| **Streaming** | ✓ (tool responses) | ✓ (full responses) |

## Message History Format

Each agent has its own message format (automatically normalized by agent_factory):

**Bedrock**:
```json
[
  {"role": "user", "content": [{"text": "Turn on the light"}]},
  {"role": "assistant", "content": [{"text": "Done!"}]}
]
```

**Gemini**:
```json
[
  {"role": "user", "parts": [{"text": "Turn on the light"}]},
  {"role": "model", "parts": [{"text": "Done!"}]}
]
```

The agent_factory preserves format per provider — no conversion needed between history and agent.

## Tool Calling

Both agents support the same tools:
- `list_devices` — Return device catalog with capabilities
- `list_scenes` — Return active scenes
- `execute_device_command` — Execute action on device with policy enforcement
- `execute_scene` — Execute scene (multiple devices)

Both agents:
- Enforce policies before execution
- Record learning phrases
- Graph events for behavior tracking
- Support up to 10 tool-call rounds

## Error Handling

### Bedrock Agent Fails
- If AGENT_PROVIDER=bedrock: Hard failure, SMS not sent
- If AGENT_PROVIDER=auto: Falls back to Gemini Live API

### Gemini Agent Fails
- If AGENT_PROVIDER=gemini: Hard failure, SMS not sent
- If AGENT_PROVIDER=auto: Falls back to... (none, would hard fail)

**Recommendation**: Use `auto` mode with both agents configured, or explicitly choose one.

## Monitoring

### Check which agent is active

```bash
# CloudWatch Logs
aws logs tail /aws/lambda/deviceweave-sms-prod --follow

# Look for:
# "Using agent provider: bedrock" → Bedrock Converse
# "Using agent provider: gemini" → Gemini Live API
# "Internet probe ... → reachable" → Auto chose Bedrock
# "Internet probe ... → unreachable" → Auto chose Gemini
```

### Check agent response time

Both agents log response latency:
```bash
aws logs tail /aws/lambda/deviceweave-sms-prod \
  --filter-pattern '"Agent finished"' --follow
```

Example:
```
Agent finished: rounds=1 session_messages=4
```

## Cost Estimation

### Typical SMS Conversation (3 messages)

**Bedrock Agent**:
- 3 invocations × ~1200 input tokens average
- 3600 input tokens × $0.00080/1K = $0.0029
- ~$0.003 per conversation (5-6 SMS pairs)

**Gemini Agent**:
- 3600 input tokens × $0.0000075/1K = $0.000027
- ~$0.00003 per conversation
- **~100x cheaper**

### Monthly (100 SMS conversations/month)

**Bedrock**: 100 × $0.003 = $0.30
**Gemini**: 100 × $0.00003 = $0.003

## Troubleshooting

### "google-genai SDK not installed"
```bash
pip install -r src/requirements.txt
# or: pip install google-genai>=0.3.0
```

### Gemini agent fails: "Failed to load Gemini API key"
1. Check Secrets Manager secret exists:
   ```bash
   aws secretsmanager get-secret-value --secret-id gemini/api_key
   ```
2. Verify secret format (must have "api_key" key):
   ```bash
   aws secretsmanager get-secret-value --secret-id gemini/api_key \
     --query SecretString --output text | jq .
   ```
3. Check Lambda IAM permissions include `secretsmanager:GetSecretValue` on the secret

### Auto mode not switching to Gemini
- Ensure GEMINI_SECRET_NAME env var is set
- Verify Gemini API key is valid
- Check CloudWatch logs for "Internet probe" messages
- Manual override: `AGENT_PROVIDER=gemini`

### Mixed history formats (shouldn't happen)
- agent_factory normalizes format per provider
- SMS handler always uses agent_factory.run_agent()
- If mixing agents manually, ensure format matches before saving history

## Advanced: Custom Agents

To add a third agent provider:

1. Create `src/my_agent.py`:
   ```python
   async def run_agent(
       user_message: str,
       history: List[Dict[str, Any]],
       system_prompt_extra: str = "",
   ) -> Tuple[str, List[Dict[str, Any]]]:
       # Implement agentic loop
       # Return (reply_text, updated_history)
   ```

2. Update `agent_factory.py`:
   ```python
   async def run_agent(...):
       if provider == "my_provider":
           return await _run_my_agent(...)
   
   def _select_provider():
       if _AGENT_PROVIDER == "my_provider":
           return "my_provider"
   ```

3. Update `template.yaml`:
   ```yaml
   AgentProvider:
     AllowedValues:
       - my_provider  # Add here
   ```

## References

- [Bedrock Converse API](https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html)
- [Google Gemini Live API](https://ai.google.dev/api/rest/google.ai.generativelanguage.v1alpha/rest/google.ai.generativelanguage.v1alpha.generativelanguage/stream-generate-content)
- [google-genai SDK](https://github.com/googleapis/python-genai)
- [Agent Factory Implementation](src/agent_factory.py)
- [Bedrock Agent Implementation](src/bedrock_agent.py)
- [Gemini Agent Implementation](src/gemini_agent.py)
