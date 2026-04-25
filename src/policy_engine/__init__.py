"""
Policy Engine — runtime enforcement layer for DeviceWeave.

Sits between device resolution and device execution.  Before any I/O
reaches a physical device, every resolved (device_type, action) pair is
checked against the active Policy DSL rules stored in DynamoDB.

Pipeline position:

  Intent Engine → Device Resolution
                         ↓
              ┌─ Policy Engine ──────────────────┐
              │  1. Load active policies (cache)  │
              │  2. Gather runtime context        │
              │     (temperature, time, is_home)  │
              │  3. Evaluate conditions           │
              │  4. Compute verdict               │
              │     BLOCK  → 403, no I/O          │
              │     MODIFY → updated params       │
              │     ALLOW  → pass-through         │
              └───────────────────────────────────┘
                         ↓
              Filtered Execution Steps
                         ↓
              Device Execution (providers/)
"""
