#!/usr/bin/env python3
"""Resolve every API and data-store coordinate a harness needs from the stack.

The two that are easy to miss are the GSI names. Both of this service's
interesting access patterns run through an index rather than the table key:

* the device registry is keyed (device_id, provider), so "every active device
  from this provider" is only answerable through ``provider-status-index``;
* the policy table is keyed (rule_id, version), so ``?device_type=`` on
  GET /policies runs through ``device-type-created-index``.

A caller who does not name an index gets no error -- just a scan. Both names
are stack Outputs now, and ``tests/test_openapi_contract.py`` asserts that
template.yaml really defines an index by each name.

Usage
-----
    python scripts/stack_env.py --stack deviceweave
    eval "$(python scripts/stack_env.py --stack deviceweave --format sh)"

    curl -sS "$DEVICEWEAVE_API_BASE/health"
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys

REQUIRED_OUTPUTS = {
    "DEVICEWEAVE_API_BASE": "ApiBaseUrl",
    "DEVICEWEAVE_REGISTRY_TABLE": "DeviceRegistryTableName",
    "DEVICEWEAVE_REGISTRY_PROVIDER_STATUS_INDEX": "DeviceRegistryProviderStatusIndex",
    "DEVICEWEAVE_LEARNING_TABLE": "LearningTableName",
    "DEVICEWEAVE_POLICY_TABLE": "PolicyTableName",
    "DEVICEWEAVE_POLICY_DEVICE_TYPE_INDEX": "PolicyTableDeviceTypeCreatedIndex",
    "DEVICEWEAVE_PRESENCE_TABLE": "PresenceTableName",
}

OPTIONAL_OUTPUTS = {
    # Holds deletions, not scenes -- see the output's own description.
    "DEVICEWEAVE_SCENE_TABLE": "SceneTableName",
    "DEVICEWEAVE_CONVERSATION_TABLE": "ConversationTableName",
    "DEVICEWEAVE_FUNCTION_ARN": "FunctionArn",
    "DEVICEWEAVE_INGESTION_FUNCTION_ARN": "IngestionFunctionArn",
    "DEVICEWEAVE_POLICY_FUNCTION_ARN": "PolicyAuthoringFunctionArn",
    "DEVICEWEAVE_LAMBDA_SECURITY_GROUP_ID": "LambdaSecurityGroupId",
    "DEVICEWEAVE_OPENAPI_SPEC": "OpenApiSpecPath",
}


class MissingOutputs(RuntimeError):
    """The stack exists but does not publish something a harness needs."""


def build_env(outputs: dict) -> dict:
    """Map stack outputs onto environment variable names (pure, so it is testable)."""
    env = {}
    missing = []
    for var, key in REQUIRED_OUTPUTS.items():
        if outputs.get(key):
            env[var] = outputs[key]
        else:
            missing.append(key)
    if missing:
        raise MissingOutputs(
            "stack publishes no value for: " + ", ".join(sorted(missing))
            + ". Deploy a template that exports them (see the Outputs section of "
            "template.yaml) -- do not hardcode them in the harness."
        )
    for var, key in OPTIONAL_OUTPUTS.items():
        if outputs.get(key):
            env[var] = outputs[key]
    return env


def fetch_outputs(stack: str, region: str | None) -> dict:
    try:
        import boto3  # noqa: PLC0415 -- optional; the CLI path covers images without it
    except ImportError:
        boto3 = None

    if boto3 is not None:
        cfn = boto3.client("cloudformation", region_name=region) if region else boto3.client("cloudformation")
        stacks = cfn.describe_stacks(StackName=stack)["Stacks"]
    else:
        cmd = ["aws", "cloudformation", "describe-stacks", "--stack-name", stack, "--output", "json"]
        if region:
            cmd += ["--region", region]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd)} failed ({proc.returncode}): {proc.stderr.strip()}")
        stacks = json.loads(proc.stdout)["Stacks"]

    return {o["OutputKey"]: o.get("OutputValue", "") for o in stacks[0].get("Outputs", [])}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Resolve DeviceWeave coordinates from the stack")
    ap.add_argument("--stack", default="deviceweave", help="CloudFormation stack name")
    ap.add_argument("--region", default=None)
    ap.add_argument("--format", choices=("json", "sh"), default="json")
    args = ap.parse_args(argv)

    try:
        env = build_env(fetch_outputs(args.stack, args.region))
    except (MissingOutputs, RuntimeError) as exc:
        print(f"stack_env: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        print(json.dumps(env, indent=2, sort_keys=True))
    else:
        for var in sorted(env):
            print(f"export {var}={shlex.quote(env[var])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
