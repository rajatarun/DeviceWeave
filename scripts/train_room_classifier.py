#!/usr/bin/env python3
"""Train the check-in room classifier and store it in DynamoDB.

Reads labeled rows from ``ROOM_CHECKIN_TABLE`` (or ``--fixture`` JSON for
offline runs), evaluates leave-one-out accuracy with a confusion matrix, and
writes the model to ``ROOM_FINGERPRINT_TABLE`` under
``pk=classifier#<person>``, ``sk=model#v1``.

Examples::

  export ROOM_CHECKIN_TABLE=deviceweave-room-checkins-dev
  export ROOM_FINGERPRINT_TABLE=deviceweave-room-fingerprints-dev
  python scripts/train_room_classifier.py --person Raja

  python scripts/train_room_classifier.py --person Raja \\
      --fixture tests/fixtures/room_checkins_sample.json --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import aws_clients  # noqa: E402
import room_classifier  # noqa: E402


def _load_fixture(path: Path) -> list:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit("fixture must be a JSON array of check-in records")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--person", default="Raja", help="Person to train for")
    parser.add_argument("--timezone", default=room_classifier.DEFAULT_TIMEZONE)
    parser.add_argument("--fixture", type=Path, help="JSON check-ins instead of DynamoDB")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate only; do not write")
    args = parser.parse_args()

    if args.fixture:
        checkins = _load_fixture(args.fixture)
    else:
        table_name = os.environ.get("ROOM_CHECKIN_TABLE", "").strip()
        if not table_name:
            print("ROOM_CHECKIN_TABLE is unset and no --fixture was given", file=sys.stderr)
            return 2
        table = aws_clients.get_dynamodb_resource().Table(table_name)
        checkins = room_classifier.training_checkins_from_table(table, person=args.person)

    try:
        model = room_classifier.train_from_checkins(
            checkins,
            person=args.person,
            timezone_name=args.timezone,
        )
    except room_classifier.TrainError as exc:
        print(f"training failed: {exc}", file=sys.stderr)
        return 1

    metrics = model.get("metrics") or {}
    print(room_classifier.format_confusion(metrics))
    print(f"stored_sample_count={model.get('sample_count')} rooms={model.get('rooms')}")

    if args.dry_run:
        return 0

    fp_table_name = os.environ.get("ROOM_FINGERPRINT_TABLE", "").strip()
    if not fp_table_name:
        print("ROOM_FINGERPRINT_TABLE is unset; use --dry-run to evaluate only", file=sys.stderr)
        return 2
    fp_table = aws_clients.get_dynamodb_resource().Table(fp_table_name)
    updated_at = datetime.now(timezone.utc).isoformat()
    room_classifier.save_model(fp_table, args.person, model, updated_at)
    print(f"model written pk={room_classifier.classifier_pk(args.person)} sk={room_classifier.MODEL_SK}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
