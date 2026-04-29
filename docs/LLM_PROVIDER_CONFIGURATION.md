# LLM Provider Configuration Guide

This document explains how DeviceWeave supports multiple LLM providers and how to configure them.

## Provider Architecture

DeviceWeave uses **two distinct patterns** for LLM integration:

### 1. Agentic Loop (bedrock_agent.py)
- **Required Provider**: **Bedrock only**
- **Why**: Uses Bedrock's native `Converse API` with tool-calling support
- **Invoked for**: SMS conversations via SMS handler
- **Configuration**: 
  - `LLM_MODEL_ID` — Bedrock cross-region inference profile
  - Always uses `bedrock-runtime` client

### 2. Single-Turn LLM Calls (other modules)
- **Configurable Provider**: Bedrock, Gemini, or Ollama
- **Invoked for**: 
  - `llm_resolver.py` — semantic device/action resolution
  - `policy_authoring.py` — natural language → policy DSL compilation
- **Configuration**: `LLM_PROVIDER` environment variable

## Environment Variables

### Agent Configuration (Bedrock-only)
```
LLM_MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0  # Bedrock model
AWS_REGION=us-east-1                                      # Bedrock region
```

### Provider Selection for Single-Turn Calls
```
LLM_PROVIDER=gemini  # Choose: auto, bedrock, gemini, ollama
```

#### auto (default fallback behavior)
```
LLM_PROVIDER=auto
```
- Probes outbound internet connectivity with TCP connection to 1.1.1.1:443
- If reachable → uses Bedrock
- If unreachable → falls back to Gemini
- Result is cached for 60 seconds to avoid repeated socket calls
- **Recommended for**: Deployments with inconsistent network access (e.g., VPC with variable NAT instance uptime)

#### bedrock
```
LLM_PROVIDER=bedrock
LLM_MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0
```
- Always use Bedrock for non-agentic calls
- Requires outbound internet access (cross-region inference)
- **Cost**: ~$0.000080 per 1K input tokens

#### gemini (RECOMMENDED)
```
LLM_PROVIDER=gemini
GEMINI_MODEL=gemini-2.0-flash  # or gemini-1.5-pro
# GEMINI_SECRET_NAME is hardcoded to: gemini/api_key
```
- Always use Gemini via API
- **Requires**: Secrets Manager secret `gemini/api_key` with API key
  - Secret format: `{"api_key": "...key..."}`
  - Create: `aws secretsmanager create-secret --name gemini/api_key --secret-string '{"api_key": "YOUR_KEY"}'`
  - Retrieve: `aws secretsmanager get-secret-value --secret-id gemini/api_key`
- **Cost**: More affordable than Bedrock for non-agentic use
- **Recommended for**: Cost-optimized deployments

#### ollama
```
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=mistral
```
- Use local Ollama instance (development/offline)
- **Requires**: Ollama server accessible from Lambda (private VPC endpoint or NAT)

## CloudFormation Parameters

All parameters have sensible defaults:

```yaml
Parameters:
  LLMProvider:
    Type: String
    Default: "gemini"
    AllowedValues: [auto, bedrock, gemini, ollama]

  LLMModelId:
    Type: String
    Default: "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    # Bedrock model ID (ignored if LLMProvider != bedrock|auto)

  GeminiModelId:
    Type: String
    Default: "gemini-2.0-flash"
    # Gemini model (ignored if LLMProvider != gemini|auto)
    # Note: Gemini API key secret is hardcoded to "gemini/api_key"
```

### To change provider after deployment:

**Via AWS Console:**
1. Go to CloudFormation → DeviceWeave stack
2. Update stack → Next
3. Change LLMProvider parameter
4. Review and submit

**Via AWS CLI:**
```bash
aws cloudformation update-stack \
  --stack-name deviceweave-prod \
  --use-previous-template \
  --parameters \
    ParameterKey=StageName,UsePreviousValue=true \
    ParameterKey=VpcId,UsePreviousValue=true \
    ParameterKey=LambdaSubnetIds,UsePreviousValue=true \
    ParameterKey=LLMProvider,ParameterValue=gemini
```

**Lambda environment variables are immutable during runtime:**
- Changes require stack update (applies after Lambda warm-start)
- Current invocations continue with old config until container restarts
- Next deployment picks up new config immediately

## Recommended Configuration

### Cost-Optimized (Gemini primary)
```
LLM_PROVIDER=gemini
GEMINI_MODEL=gemini-2.0-flash
```
- Agent uses Bedrock Converse API (required)
- All other calls use Gemini (cheaper)
- **Cost**: ~$0.0003 per SMS message (inference only, no SDK cost)

### Bedrock Primary (always online)
```
LLM_PROVIDER=bedrock
LLM_MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0
```
- All calls use Bedrock
- Requires stable outbound internet
- **Cost**: ~$0.001 per SMS message (cross-region inference overhead)

### Hybrid/Auto (adaptive)
```
LLM_PROVIDER=auto
```
- Uses Bedrock when internet is reachable
- Falls back to Gemini when offline
- **Recommended for**: VPCs with variable NAT availability
- **Trade-off**: 2-second TCP probe latency on cache miss

## IAM Permissions

All Lambda functions have permission to access:
- **Bedrock**: `bedrock:InvokeModel` + `bedrock:Converse`
- **Gemini Secret**: `secretsmanager:GetSecretValue` on `${GeminiSecretName}`
- **Ollama**: No AWS permissions needed (VPC access only)

## Monitoring

### Check which provider is being used
```bash
# SSH to Lambda CloudWatch logs
aws logs tail /aws/lambda/deviceweave-handler-prod --follow

# Look for:
# - "Bedrock provider initialised" → Bedrock active
# - "Gemini provider initialised" → Gemini active
# - "Internet probe 1.1.1.1:443 → reachable" → Auto chose Bedrock
# - "Internet probe 1.1.1.1:443 → unreachable" → Auto chose Gemini
```

### Test a provider without deploying
```python
# Local test
import os
os.environ["LLM_PROVIDER"] = "gemini"
os.environ["GEMINI_SECRET_NAME"] = "gemini/api_key"

from llm_provider import get_llm_provider
provider = get_llm_provider()
result = provider.invoke(system_prompt, user_message, max_tokens=256)
print(result)
```

## Migration Guide

### From Bedrock-only to Gemini primary
1. Create Gemini API key at [Google AI Studio](https://aistudio.google.com/apikey)
2. Store in Secrets Manager:
   ```bash
   aws secretsmanager create-secret \
     --name gemini/api_key \
     --secret-string '{"api_key": "YOUR_API_KEY"}'
   ```
3. Update deployment:
   ```bash
   # Via GitHub Actions secrets
   gh secret set LLM_PROVIDER --body gemini
   
   # Or via SAM parameter override
   sam deploy --parameter-overrides LLMProvider=gemini
   ```
4. Monitor logs for provider selection and errors

### Rollback (if Gemini fails)
```bash
gh secret set LLM_PROVIDER --body bedrock
# Or set to "auto" for adaptive fallback
```

## References

- [DeviceWeave LLM Provider Implementation](src/llm_provider/__init__.py)
- [Bedrock Converse API](https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html)
- [Google Gemini API](https://ai.google.dev/docs)
- [Ollama](https://ollama.ai)
