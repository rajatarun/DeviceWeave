"""Validate one OBSERVATORY_METRICS item against the shared table contract.

This module is deliberately dependency-free (no jsonschema, no boto3) so that
every consumer repository can vendor it next to its copy of
``observatory_metrics_item.json`` and run the same checks its siblings run.
The table is written by services in two languages that cannot import each
other's code, so the only thing keeping their rows mutually legible is that
each one checks itself against this file.

Usage in a consumer repository::

    from contracts.conformance import load_contract, check_item
    problems = check_item(emitted_item, load_contract())
    assert problems == [], problems
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

CONTRACT_FILENAME = "observatory_metrics_item.json"

# "{iso8601}#{trace_id}" -- the timestamp must sort first, so it is anchored.
_SK_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?#.+$")
_PK_RE = re.compile(r"^([A-Z_]+)#(.+)$")


def load_contract(path: str | Path | None = None) -> dict[str, Any]:
    """Read the contract JSON sitting beside this module unless told otherwise."""
    target = Path(path) if path else Path(__file__).with_name(CONTRACT_FILENAME)
    with open(target, encoding="utf-8") as fh:
        return json.load(fh)


def _unwrap(value: Any) -> Any:
    """Accept both the resource-level item shape and the low-level AttributeValue one.

    Python writers use ``boto3.resource(...).Table.put_item`` and emit plain
    values; the Node writer uses the low-level client and emits ``{"S": ...}``.
    Both must be checkable by the same function or the two languages drift.
    """
    if isinstance(value, dict) and len(value) == 1:
        tag, inner = next(iter(value.items()))
        if tag in {"S", "N", "BOOL", "NULL"}:
            return inner
    return value


def check_item(item: dict[str, Any], contract: dict[str, Any] | None = None) -> list[str]:
    """Return a list of contract violations; empty means the item conforms.

    Returning problems rather than raising lets a caller report every violation
    in one go, which matters when a writer is wrong about several things at
    once (a wrong key spelling usually travels with a wrong namespace).
    """
    contract = contract or load_contract()
    problems: list[str] = []

    # I1 -- key attributes are spelled in lower case.
    for wrong, right in (("PK", "pk"), ("SK", "sk")):
        if wrong in item and right not in item:
            problems.append(
                f"I1: item uses '{wrong}' but the table's key attribute is '{right}'; "
                "DynamoDB attribute names are case sensitive, so PutItem rejects this "
                "item with a ValidationException"
            )
    for required in (contract["key_schema"]["partition_key"], contract["key_schema"]["sort_key"]):
        if required not in item:
            problems.append(f"I1: required key attribute '{required}' is missing")

    pk = _unwrap(item.get("pk"))
    sk = _unwrap(item.get("sk"))

    # I2 -- pk carries a registered namespace.
    if isinstance(pk, str):
        match = _PK_RE.match(pk)
        if not match:
            problems.append(f"I2: pk {pk!r} does not match '{{namespace}}#{{discriminator}}'")
        elif match.group(1) not in contract["namespace_registry"]:
            problems.append(
                f"I2: pk namespace {match.group(1)!r} is not in the contract's "
                f"namespace_registry {sorted(contract['namespace_registry'])}; a row in an "
                "unregistered partition is invisible to every reader"
            )

    # I3 -- sk sorts by time.
    if isinstance(sk, str) and not _SK_RE.match(sk):
        problems.append(
            f"I3: sk {sk!r} does not match '{{iso8601}}#{{trace_id}}'; readers range-query "
            "sk lexicographically, so a non-ISO or non-leading timestamp breaks time filters"
        )

    # I4 -- rows expire.
    if "ttl" not in item:
        problems.append("I4: no 'ttl' attribute; rows would accumulate in a shared table forever")

    return problems


def readers_for(pk: str, contract: dict[str, Any] | None = None) -> list[str]:
    """Which readers, if any, will ever see a row written at this pk.

    An empty list is the machine-readable form of "this telemetry is written,
    billed, and never read by anything".
    """
    contract = contract or load_contract()
    match = _PK_RE.match(pk or "")
    if not match:
        return []
    entry = contract["namespace_registry"].get(match.group(1))
    if not entry:
        return []
    if entry["discriminator"] == "operation":
        allowed = entry.get("discriminator_values") or []
        if allowed and match.group(2) not in allowed:
            # Registered namespace, but a discriminator no reader enumerates.
            return []
    return list(entry.get("readers") or [])
