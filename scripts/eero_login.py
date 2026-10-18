#!/usr/bin/env python3
"""
One-time eero login for DeviceWeave, plus tracked-device registration.

eero sends a one-time code by SMS or email. This script logs in, verifies
the code, and stores the session token in Secrets Manager. It does not print
the token. The presence poller reads that secret with GetSecretValue.

The eero API used here is unofficial and can change. It does not support
Amazon sign-in. Log in as a separate standard eero account (email or phone
and a one-time code) that the owner has invited as a network admin. See
docs/eero-presence.md.

Examples
--------
  python scripts/eero_login.py
  python scripts/eero_login.py login --secret-name deviceweave/eero-session
  python scripts/eero_login.py track --person Raja --mac aa:bb:cc:dd:ee:ff
  python scripts/eero_login.py track --person Raja --name "Raja phone"

``aa:bb:cc:dd:ee:ff`` above is a placeholder, not a device on any network.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import eero_client  # noqa: E402
import eero_presence  # noqa: E402

DEFAULT_SECRET = "deviceweave/eero-session"
DEFAULT_STAGE = "prod"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")

    login_parser = sub.add_parser("login", help="Request a code, verify it, store the session")
    _add_common(login_parser)
    login_parser.add_argument(
        "--identifier",
        help="Dedicated admin email or phone (not an Amazon login). Prompted when omitted.",
    )
    login_parser.add_argument("--code", help="Verification code. Prompted when omitted.")

    track_parser = sub.add_parser("track", help="Map one eero client to a person")
    _add_common(track_parser)
    track_parser.add_argument("--person", required=True, help="Person this device belongs to, for example Raja")
    track_parser.add_argument("--stage", default=DEFAULT_STAGE, help="Stack stage used in the default table name")
    track_parser.add_argument("--table", help="Tracked-device table. Default deviceweave-eero-tracked-<stage>")
    choice = track_parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--mac", help="Client MAC. Example placeholder: aa:bb:cc:dd:ee:ff")
    choice.add_argument("--name", help="Client nickname, hostname, or display name. Exact, case-insensitive.")

    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args(["login"])
    try:
        if args.command == "track":
            return _track(args)
        return _login(args)
    except eero_client.EeroAuthError as exc:
        print(f"eero login failed: {exc}", file=sys.stderr)
        return 1
    except eero_client.EeroError as exc:
        print(f"eero request failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("", file=sys.stderr)
        return 130
    except ImportError:
        print("boto3 is required to store the session or a tracked device", file=sys.stderr)
        return 1
    except Exception as exc:
        # Do not print the exception: a failed Secrets Manager call can echo the token.
        code = ""
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            code = str((response.get("Error") or {}).get("Code") or "")
        detail = f" ({code})" if code else ""
        print(f"setup failed{detail}: {exc.__class__.__name__}", file=sys.stderr)
        return 1


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--secret-name", default=DEFAULT_SECRET, help=f"Default {DEFAULT_SECRET}")
    parser.add_argument("--region", default=None, help="AWS region. Default AWS_REGION, else us-east-1")


def _login(args: argparse.Namespace) -> int:
    secret_name = (args.secret_name or "").strip()
    if not secret_name:
        print("secret name must not be blank", file=sys.stderr)
        return 2
    identifier = (args.identifier or "").strip() or input(
        "Dedicated admin email or phone (not an Amazon login): "
    ).strip()
    if not identifier:
        print("email or phone is required", file=sys.stderr)
        return 2
    print(
        "Requesting a one-time code for the dedicated admin account. "
        "Do not use an Amazon login."
    )
    user_token = eero_client.login(identifier)
    code = (args.code or "").strip() or input("Verification code: ").strip()
    if not code:
        print("verification code is required", file=sys.stderr)
        return 2
    session = eero_client.login_verify(code, user_token)
    _store_secret(secret_name, session, _region(args))
    print(f"Stored eero session in Secrets Manager secret {secret_name}.")
    print("The session token is not printed. Re-run this command if a later poll reports a rejected session.")
    return 0


def _track(args: argparse.Namespace) -> int:
    person = (args.person or "").strip()
    if not person:
        print("person must not be blank", file=sys.stderr)
        return 2
    table_name = (args.table or f"deviceweave-eero-tracked-{args.stage}").strip()
    if args.mac:
        mac = args.mac.strip()
        normalized = eero_presence.normalize_mac(mac)
        if len(normalized) != 12:
            print("mac must be 12 hex digits (separators optional)", file=sys.stderr)
            return 2
        item = {
            "pk": f"mac:{normalized}",
            "person": person,
            "match": "mac",
            "mac": mac,
        }
        described = "mac"
    else:
        name = (args.name or "").strip()
        if not name:
            print("name must not be blank", file=sys.stderr)
            return 2
        item = {
            "pk": f"name:{name.casefold()}",
            "person": person,
            "match": "name",
            "name": name,
        }
        described = f"name {name}"
    _put_tracked(table_name, item, _region(args))
    print(f"Tracking {person} by {described} in {table_name}.")
    return 0


def _store_secret(secret_name: str, session_token: str, region: str) -> None:
    import boto3
    from botocore.exceptions import ClientError

    client = boto3.client("secretsmanager", region_name=region)
    payload = json.dumps({"session_token": session_token})
    try:
        client.put_secret_value(SecretId=secret_name, SecretString=payload)
    except ClientError as exc:
        if (exc.response.get("Error") or {}).get("Code") != "ResourceNotFoundException":
            raise
        client.create_secret(
            Name=secret_name,
            SecretString=payload,
            Description="DeviceWeave eero session token for the unofficial cloud API. Written by scripts/eero_login.py.",
        )


def _put_tracked(table_name: str, item: dict, region: str) -> None:
    import boto3

    table = boto3.resource("dynamodb", region_name=region).Table(table_name)
    table.put_item(Item=item)


def _region(args: argparse.Namespace) -> str:
    import os

    return args.region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"


if __name__ == "__main__":
    sys.exit(main())
