"""VPC Lambdas stay on the private subnets, and are dual-stack when those subnets allow it."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "template.yaml"
WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"


class _CfnLoader(yaml.SafeLoader):
    """safe_load that keeps CloudFormation short tags as {'!Tag': value}."""


def _tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {"!" + suffix: value}


_CfnLoader.add_multi_constructor("!", _tag)


def _template():
    return yaml.load(TEMPLATE.read_text(), Loader=_CfnLoader)


def test_every_vpc_function_sets_dual_stack_from_the_parameter():
    t = _template()
    assert t["Parameters"]["LambdaIpv6DualStack"]["Default"] == "true"
    assert t["Conditions"]["LambdaDualStack"] == {"!Equals": [{"!Ref": "LambdaIpv6DualStack"}, "true"]}
    vpc_functions = {name: r["Properties"]["VpcConfig"] for name, r in t["Resources"].items()
                     if r["Type"] in ("AWS::Serverless::Function", "AWS::Lambda::Function")
                     and "VpcConfig" in r.get("Properties", {})}
    assert len(vpc_functions) >= 4
    for name, cfg in vpc_functions.items():
        assert cfg.get("Ipv6AllowedForDualStack") == {"!If": ["LambdaDualStack", True, False]}, name


def test_deploy_passes_the_parameter_from_the_subnet_check():
    wf = WORKFLOW.read_text()
    assert '"LambdaIpv6DualStack=${{ steps.dualstack.outputs.dual_stack' in wf
    assert "id: dualstack" in wf


def test_public_subnet_override_is_opt_in_and_defaults_to_private():
    """Empty LambdaPublicSubnetIds keeps the private subnets. A non-empty list
    does not move functions unless LambdaUsePublicSubnets is also true."""
    t = _template()
    public = t["Parameters"]["LambdaPublicSubnetIds"]
    flag = t["Parameters"]["LambdaUsePublicSubnets"]
    assert public["Default"] == ""
    assert flag["Default"] == "false"
    assert flag["AllowedValues"] == ["true", "false"]
    for text in (public["Description"], flag["Description"]):
        assert "Public subnets have no DynamoDB/AWS-service egress and need NAT or endpoints" in text
    assert t["Conditions"]["HasPublicSubnets"] == {
        "!And": [
            {"!Not": [{"!Equals": [{"!Join": ["", {"!Ref": "LambdaPublicSubnetIds"}]}, ""]}]},
            {"!Equals": [{"!Ref": "LambdaUsePublicSubnets"}, "true"]},
        ]
    }
    for name, resource in t["Resources"].items():
        cfg = resource.get("Properties", {}).get("VpcConfig")
        if not cfg:
            continue
        assert cfg["SubnetIds"] == {
            "!If": ["HasPublicSubnets", {"!Ref": "LambdaPublicSubnetIds"}, {"!Ref": "LambdaSubnetIds"}]
        }, name


def test_deploy_clears_public_subnets_and_does_not_discover_them():
    wf = WORKFLOW.read_text()
    assert "map-public-ip-on-launch" not in wf
    assert "id: public_subnets" not in wf
    assert "LambdaPublicSubnetIds=${{" not in wf
    # SAM rejects a bare Key= with an empty value. The quoted long form is
    # what CfnParameterOverridesType accepts for an empty override.
    assert 'ParameterKey=LambdaPublicSubnetIds,ParameterValue=""' in wf
    assert "LambdaUsePublicSubnets=false" in wf
    step = next(s for s in yaml.safe_load(wf)["jobs"]["deploy"]["steps"] if s.get("id") == "dualstack")
    assert "public_subnet" not in step["run"]
    assert "steps.vpc.outputs.subnet_ids" in step["run"]


# aws-sam-cli==1.121.0 samcli/cli/types.py — CfnParameterOverridesType.
# The deploy workflow pins that version. This copy runs without installing
# SAM. test_vendored_parser_matches_sam_cli checks it against the package
# when that exact version is installed.
_PARAM_AND_METADATA_KEY_REGEX = """([A-Za-z0-9\\"\']+)"""


def _generate_match_regex(match_pattern, delim):
    return (
        f"""(\\"(?:\\\\{match_pattern}|[^\\"\\\\]+)*\\"|"""
        + f"""\'(?:\\\\{match_pattern}|[^\'\\\\]+)*\'|"""
        + f"""(?:\\\\{match_pattern}|[^{delim}\\"\\\\]+)+)"""
    )


def _unquote_wrapped_quotes(value):
    if value and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1]
    return value.replace("\\ ", " ").replace('\\"', '"').replace("\\'", "'")


def parse_cfn_parameter_overrides(values):
    """Space-delimited ParameterKey=...,ParameterValue=... or Key=Value."""
    value_regex = _generate_match_regex(".", " ")
    patterns = [
        rf"(?:ParameterKey={_PARAM_AND_METADATA_KEY_REGEX},ParameterValue={value_regex})",
        rf"(?:(?: ){_PARAM_AND_METADATA_KEY_REGEX}={value_regex})",
    ]
    result = {}
    if values == ("",):
        return result
    seq = (values,) if isinstance(values, str) else values
    for val in seq:
        normalized = " " + val.strip()
        pattern = next((item for item in patterns if re.findall(item, normalized)), None)
        if pattern is None:
            raise ValueError(f"{val} is not a valid parameter override")
        for key, param_value in re.findall(pattern, normalized):
            result[_unquote_wrapped_quotes(key)] = _unquote_wrapped_quotes(param_value)
    return result


def _sam_deploy_argv(tmp_path) -> list[str]:
    """Shell-split the workflow's sam deploy line the way Actions will."""
    wf = yaml.safe_load(WORKFLOW.read_text())
    step = next(s for s in wf["jobs"]["deploy"]["steps"] if s.get("name") == "SAM deploy")
    script = re.sub(r"\$\{\{.*?\}\}", "gha-value", step["run"])
    fake = tmp_path / "bin"
    fake.mkdir()
    out = tmp_path / "argv"
    (fake / "sam").write_text("#!/bin/bash\n" f'printf "%s\\0" "$@" > "{out}"\n')
    (fake / "sam").chmod(0o755)
    env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}",
           "STAGE": "prod", "STACK_NAME": "deviceweave-prod", "AWS_REGION": "us-east-1"}
    proc = subprocess.run(["bash", "-e", "-c", script], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    args = [a.decode() for a in out.read_bytes().split(b"\0") if a]
    assert args[0] == "deploy", args
    return args


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_sam_parameter_overrides_parser_accepts_the_workflow_string(tmp_path):
    """Run SAM CLI 1.x CfnParameterOverridesType on the exact argv bash produces."""
    pytest.importorskip("samcli")
    import click
    from click.testing import CliRunner
    from samcli.commands._utils.options import parameter_override_click_option

    argv = _sam_deploy_argv(tmp_path)
    overrides = argv[argv.index("--parameter-overrides") + 1:]
    assert 'ParameterKey=LambdaPublicSubnetIds,ParameterValue=""' in overrides
    assert "LambdaPublicSubnetIds=" not in overrides

    @click.command()
    @parameter_override_click_option()
    def _cmd(parameter_overrides):
        click.echo(json.dumps(parameter_overrides))

    result = CliRunner().invoke(_cmd, ["--parameter-overrides", *overrides])
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed == parse_cfn_parameter_overrides(overrides)
    _assert_deploy_overrides(parsed, overrides)


def _assert_deploy_overrides(parsed: dict, overrides: list[str]) -> None:
    assert parsed["EeroPollSchedule"] == "rate(5 minutes)"
    assert parsed["LambdaPublicSubnetIds"] == ""
    assert parsed["LambdaUsePublicSubnets"] == "false"
    assert parsed["LambdaSubnetIds"] == "gha-value"
    assert parsed["EeroSessionSecretName"] == "deviceweave/eero-session"
    assert parsed["EeroAwayMissThreshold"] == "3"
    spaced = {key: value for key, value in parsed.items() if any(ch.isspace() for ch in value)}
    assert spaced == {"EeroPollSchedule": "rate(5 minutes)"}
    for token in overrides:
        if token.startswith("EeroPollSchedule="):
            assert token == 'EeroPollSchedule="rate(5 minutes)"'
        else:
            assert " " not in token, token


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_eero_poll_schedule_override_parses_to_the_full_value(tmp_path):
    """Shell-split the workflow, then parse it the way SAM 1.121.0 does."""
    argv = _sam_deploy_argv(tmp_path)
    overrides = argv[argv.index("--parameter-overrides") + 1:]
    parsed = parse_cfn_parameter_overrides(overrides)
    _assert_deploy_overrides(parsed, overrides)
    # The form that failed run 39145288741: bash strips the outer quotes and
    # SAM's space delimiter keeps only rate(5.
    broken = parse_cfn_parameter_overrides(["EeroPollSchedule=rate(5 minutes)"])
    assert broken["EeroPollSchedule"] == "rate(5"


def test_vendored_parser_matches_sam_cli():
    """The in-repo copy is the aws-sam-cli 1.121.0 CfnParameterOverridesType."""
    pytest.importorskip("samcli")
    import importlib.metadata

    version = importlib.metadata.version("aws-sam-cli")
    if version != "1.121.0":
        pytest.skip(f"deploy pins aws-sam-cli==1.121.0, found {version}")
    from samcli.cli.types import CfnParameterOverridesType

    samples = [
        ['EeroPollSchedule="rate(5 minutes)"'],
        ["EeroPollSchedule=rate(5 minutes)"],
        ['ParameterKey=EeroPollSchedule,ParameterValue="rate(5 minutes)"'],
        ['ParameterKey=LambdaPublicSubnetIds,ParameterValue=""', "StageName=prod"],
        ["EeroSessionSecretName=deviceweave/eero-session", "EeroAwayMissThreshold=3"],
    ]
    real = CfnParameterOverridesType()
    for sample in samples:
        assert parse_cfn_parameter_overrides(sample) == real.convert(tuple(sample), None, None)


def _check_step_script() -> str:
    wf = yaml.safe_load(WORKFLOW.read_text())
    step = next(s for s in wf["jobs"]["deploy"]["steps"] if s.get("id") == "dualstack")
    script = step["run"].replace("${{ steps.vpc.outputs.subnet_ids }}", "${PRIVATE}")
    assert "${{" not in script, script
    return script


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("private,ipv6,expected", [
    ("subnet-a,subnet-b", {"subnet-a", "subnet-b"}, "true"),
    ("subnet-a,subnet-b", {"subnet-a"}, "false"),
    ("subnet-a", {"subnet-a"}, "true"),
    ("subnet-a", set(), "false"),
])
def test_subnet_check_step_runs_under_bash_e(tmp_path, private, ipv6, expected):
    fake = tmp_path / "bin"
    fake.mkdir()
    # Fake `aws ec2 describe-subnets --subnet-ids X ...`: prints a CIDR when X has IPv6, else None.
    cases = "".join(f'    {s}) echo "2600:1f18::/64";;\n' for s in sorted(ipv6))
    (fake / "aws").write_text(
        "#!/usr/bin/env bash\n"
        'while [ "$1" != "--subnet-ids" ]; do shift; done; shift\n'
        f'case "$1" in\n{cases}    *) echo None;;\nesac\n')
    (fake / "aws").chmod(0o755)
    out = tmp_path / "out"
    env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}", "GITHUB_OUTPUT": str(out),
           "AWS_REGION": "us-east-1", "PRIVATE": private}
    proc = subprocess.run(["bash", "-e", "-c", _check_step_script()], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert f"dual_stack={expected}" in out.read_text()
    if expected == "false":
        assert "::warning::" in proc.stdout
