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
    assert parsed["LambdaPublicSubnetIds"] == ""
    assert parsed["LambdaUsePublicSubnets"] == "false"
    assert parsed["LambdaSubnetIds"] == "gha-value"


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
