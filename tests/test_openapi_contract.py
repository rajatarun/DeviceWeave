"""Keep openapi/deviceweave.yaml honest against the template.

Twenty routes across three Lambdas, and the routing lives in two places that
can disagree: template.yaml decides what API Gateway forwards, and the three
handlers decide what to do with it. This test pins the spec to the first --
method by method, both directions -- and pins the stack Outputs a harness
resolves its coordinates from to the resources that actually exist.

It also pins three behaviours the status code alone hides, because a harness
that gets them wrong fails quietly rather than loudly:

* ``POST /devices`` is routed and always 405s (devices come from ingestion);
* ``POST /ingest`` answers Ring's two-factor challenge with a **200**, so a
  check for 2xx reads "we texted you a code" as a completed sync;
* several routes return 503 for *unconfigured*, not for down -- retrying a
  missing environment variable never succeeds.

None of it needs AWS.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = REPO_ROOT / "openapi" / "deviceweave.yaml"
TEMPLATE_PATH = REPO_ROOT / "template.yaml"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import stack_env  # noqa: E402

HTTP_METHODS = ("get", "post", "put", "patch", "delete")


@pytest.fixture(scope="module")
def spec() -> dict:
    with open(SPEC_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _template_routes() -> set:
    """(path, method) pairs API Gateway forwards.

    SAM writes each Api event as a Path: line immediately followed by a
    Method: line, so this pairs them positionally rather than parsing a
    template full of !Sub and !GetAtt tags.
    """
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    return {
        (m.group(1), m.group(2).lower())
        for m in re.finditer(r"^\s+Path:\s*(\S+)\s*\n\s+Method:\s*(\S+)\s*$", text, re.M)
    }


def test_template_routes_were_actually_parsed():
    """Guard the guard: an empty parse would make the comparison vacuous."""
    routes = _template_routes()
    assert len(routes) >= 18, f"only parsed {len(routes)} routes -- the Path/Method pairing has gone stale"


def test_spec_documents_exactly_the_routes_the_gateway_forwards(spec):
    template = _template_routes()
    documented = {(p, m) for p, item in spec["paths"].items()
                  for m in item if m in HTTP_METHODS}
    only_template = sorted(template - documented)
    only_spec = sorted(documented - template)
    assert not only_template, (
        f"API Gateway forwards these, but openapi/ does not document them: {only_template}"
    )
    assert not only_spec, (
        f"openapi/ documents these, but API Gateway does not forward them "
        f"(they would 403 before reaching a handler): {only_spec}"
    )


def test_post_devices_is_documented_as_the_405_it_always_is(spec):
    """It is routed, and the handler always refuses it. Silence would be worse.

    A client that finds POST /devices and expects a create needs to see the
    405 and the pointer to POST /ingest in the spec, not at runtime.
    """
    responses = spec["paths"]["/devices"]["post"]["responses"]
    assert set(responses) == {"405"}, (
        f"POST /devices documents {sorted(responses)}; the handler returns 405 "
        f"and nothing else"
    )


def test_ingest_documents_the_two_factor_challenge_as_a_200(spec):
    """Ring's 2FA challenge is a 200, not an error.

    A harness that only checks for 2xx will record "we texted you a code" as a
    completed sync, then assert against a registry nothing was written to.
    """
    responses = spec["paths"]["/ingest"]["post"]["responses"]
    assert "200" in responses and "202" in responses, (
        f"POST /ingest documents {sorted(responses)}; it needs both -- 202 for a "
        f"completed sync and 200 for the 2FA challenge, which are different outcomes"
    )
    schema = responses["200"]["content"]["application/json"]["schema"]
    enum = schema["properties"]["status"]["enum"]
    assert set(enum) == {"2fa_required", "2fa_code_invalid"}, (
        f"the 200 response's status enum is {enum}; it should be exactly the two "
        f"challenge states the handler emits"
    )


def test_unconfigured_routes_document_503(spec):
    """503 here means "no environment variable", which retrying cannot fix."""
    for path, method in (("/learn", "post"), ("/learnings", "get"), ("/learnings", "delete"),
                         ("/presence", "get"), ("/presence", "post"),
                         ("/policies", "get"), ("/policies/author", "post"),
                         ("/policies/{rule_id}", "get"), ("/policies/{rule_id}", "delete"),
                         ("/devices", "get")):
        responses = spec["paths"][path][method]["responses"]
        assert "503" in responses, (
            f"{method.upper()} {path} returns 503 when its table is unconfigured, "
            f"but the spec documents {sorted(responses)}"
        )


def _output_names() -> set:
    """Top-level keys of template.yaml's Outputs block.

    Regex rather than yaml.safe_load: the template is full of !Sub/!Ref tags a
    safe loader rejects, and a permissive loader that maps unknown tags to None
    reports every !Sub-valued field as absent -- which is how a check like this
    ends up asserting nothing.
    """
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    block = text.split("\nOutputs:\n", 1)
    assert len(block) == 2, "template.yaml has no top-level Outputs: block"
    names = set()
    for line in block[1].splitlines():
        if line and not line.startswith(" ") and not line.startswith("#"):
            break
        m = re.match(r"^  ([A-Za-z][A-Za-z0-9]*):\s*$", line)
        if m:
            names.add(m.group(1))
    return names


def test_stack_env_reads_only_outputs_the_template_publishes():
    published = _output_names()
    assert published, "parsed no outputs from template.yaml -- the parser is broken"
    wanted = set(stack_env.REQUIRED_OUTPUTS.values()) | set(stack_env.OPTIONAL_OUTPUTS.values())
    missing = sorted(wanted - published)
    assert not missing, (
        f"scripts/stack_env.py reads stack outputs that template.yaml does not "
        f"publish: {missing}. Either add the Output or stop reading it."
    )


def test_required_outputs_cover_the_api_and_both_indexes():
    """Both interesting access patterns run through an index, not the table key."""
    required = set(stack_env.REQUIRED_OUTPUTS.values())
    for needed in ("ApiBaseUrl", "DeviceRegistryTableName",
                   "DeviceRegistryProviderStatusIndex", "PolicyTableName",
                   "PolicyTableDeviceTypeCreatedIndex", "LearningTableName",
                   "PresenceTableName"):
        assert needed in required, f"{needed} is not resolved from the stack"


def test_index_outputs_name_indexes_the_tables_define():
    """An output naming a nonexistent index fails at query time, not at deploy."""
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    defined = set(re.findall(r"^\s+- IndexName:\s*(\S+)\s*$", text, re.M))
    assert defined, "parsed no IndexName entries from template.yaml"
    for output in ("DeviceRegistryProviderStatusIndex", "PolicyTableDeviceTypeCreatedIndex"):
        m = re.search(rf"^  {output}:\n(?:.*\n)*?^    Value:\s*(\S+)\s*$", text, re.M)
        assert m, f"template.yaml has no {output} output with a literal Value"
        assert m.group(1) in defined, (
            f"{output} names index {m.group(1)!r}, which no table defines "
            f"(defined: {sorted(defined)})"
        )


def test_openapi_spec_path_output_points_at_the_spec():
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    m = re.search(r"^  OpenApiSpecPath:\n(?:.*\n)*?^    Value:\s*(\S+)\s*$", text, re.M)
    assert m, "template.yaml has no OpenApiSpecPath output with a literal Value"
    assert (REPO_ROOT / m.group(1)).is_file(), (
        f"OpenApiSpecPath points at {m.group(1)}, which does not exist"
    )
