"""
Policy Authoring System for DeviceWeave.

Converts natural language IoT automation rules into validated Policy DSL
and persists them to DynamoDB. The LLM is the sole semantic interpreter —
no regex, no string heuristics.

Pipeline:
  natural language → LLM compiler → Policy DSL JSON
                                         ↓
                                  strict validator
                                         ↓
                                   DynamoDB store
"""
