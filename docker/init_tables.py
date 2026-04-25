#!/usr/bin/env python3
"""
Creates DynamoDB Local tables for local development.
Runs once on container start; safe to re-run (skips existing tables).
"""
import os
import sys
import time

import boto3
from botocore.exceptions import ClientError

# boto3 >= 1.28 reads AWS_ENDPOINT_URL_DYNAMODB natively for the app;
# init_tables.py uses the same var so both point at the same instance.
ENDPOINT = (
    os.environ.get("AWS_ENDPOINT_URL_DYNAMODB")
    or os.environ.get("DYNAMODB_ENDPOINT_URL")
    or "http://localhost:8000"
)
REGION   = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

client = boto3.client(
    "dynamodb",
    endpoint_url=ENDPOINT,
    region_name=REGION,
    aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "local"),
    aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "local"),
)

TABLES = [
    {
        "TableName": os.environ.get("LEARNING_TABLE_NAME", "deviceweave-phrases-dev"),
        "AttributeDefinitions": [
            {"AttributeName": "device_id", "AttributeType": "S"},
            {"AttributeName": "phrase",    "AttributeType": "S"},
        ],
        "KeySchema": [
            {"AttributeName": "device_id", "KeyType": "HASH"},
            {"AttributeName": "phrase",    "KeyType": "RANGE"},
        ],
        "BillingMode": "PAY_PER_REQUEST",
    },
    {
        "TableName": os.environ.get("DEVICE_REGISTRY_TABLE", "deviceweave-registry-dev"),
        "AttributeDefinitions": [
            {"AttributeName": "device_id", "AttributeType": "S"},
            {"AttributeName": "provider",  "AttributeType": "S"},
            {"AttributeName": "status",    "AttributeType": "S"},
        ],
        "KeySchema": [
            {"AttributeName": "device_id", "KeyType": "HASH"},
            {"AttributeName": "provider",  "KeyType": "RANGE"},
        ],
        "BillingMode": "PAY_PER_REQUEST",
        "GlobalSecondaryIndexes": [
            {
                "IndexName": "provider-status-index",
                "KeySchema": [
                    {"AttributeName": "provider", "KeyType": "HASH"},
                    {"AttributeName": "status",   "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    },
    {
        "TableName": os.environ.get("PRESENCE_TABLE_NAME", "deviceweave-presence-dev"),
        "AttributeDefinitions": [
            {"AttributeName": "pk", "AttributeType": "S"},
        ],
        "KeySchema": [
            {"AttributeName": "pk", "KeyType": "HASH"},
        ],
        "BillingMode": "PAY_PER_REQUEST",
    },
    {
        "TableName": os.environ.get("POLICY_TABLE_NAME", "deviceweave-policies-dev"),
        "AttributeDefinitions": [
            {"AttributeName": "rule_id",     "AttributeType": "S"},
            {"AttributeName": "version",     "AttributeType": "N"},
            {"AttributeName": "device_type", "AttributeType": "S"},
            {"AttributeName": "created_at",  "AttributeType": "S"},
        ],
        "KeySchema": [
            {"AttributeName": "rule_id",  "KeyType": "HASH"},
            {"AttributeName": "version",  "KeyType": "RANGE"},
        ],
        "BillingMode": "PAY_PER_REQUEST",
        "GlobalSecondaryIndexes": [
            {
                "IndexName": "device-type-created-index",
                "KeySchema": [
                    {"AttributeName": "device_type", "KeyType": "HASH"},
                    {"AttributeName": "created_at",  "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    },
]


def wait_for_dynamodb(retries: int = 30, delay: float = 2.0) -> None:
    print(f"Waiting for DynamoDB Local at {ENDPOINT} ...", flush=True)
    for i in range(retries):
        try:
            client.list_tables()
            print("DynamoDB Local is ready.", flush=True)
            return
        except Exception:
            if i == retries - 1:
                print("ERROR: DynamoDB Local did not start in time.", file=sys.stderr)
                sys.exit(1)
            time.sleep(delay)


def create_tables() -> None:
    for spec in TABLES:
        name = spec["TableName"]
        try:
            client.create_table(**spec)
            print(f"  created  {name}", flush=True)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceInUseException":
                print(f"  exists   {name}", flush=True)
            else:
                raise


if __name__ == "__main__":
    wait_for_dynamodb()
    print("Creating tables...", flush=True)
    create_tables()
    print("Done.", flush=True)
