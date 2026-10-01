"""Every VPC Lambda is dual-stack when its subnets allow it, and the deploy decides that from the subnets."""
from __future__ import annotations

import os
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


def _check_step_script() -> str:
    wf = yaml.safe_load(WORKFLOW.read_text())
    step = next(s for s in wf["jobs"]["deploy"]["steps"] if s.get("id") == "dualstack")
    return (step["run"]
            .replace("${{ steps.public_subnets.outputs.public_subnet_ids }}", "${PUBLIC}")
            .replace("${{ steps.vpc.outputs.subnet_ids }}", "${PRIVATE}"))


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("public,private,ipv6,expected", [
    ("", "subnet-a,subnet-b", {"subnet-a", "subnet-b"}, "true"),
    ("", "subnet-a,subnet-b", {"subnet-a"}, "false"),
    ("subnet-p", "subnet-a", {"subnet-p"}, "true"),          # public subnets are the ones used
    ("subnet-p", "subnet-a", {"subnet-a"}, "false"),
])
def test_subnet_check_step_runs_under_bash_e(tmp_path, public, private, ipv6, expected):
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
           "AWS_REGION": "us-east-1", "PUBLIC": public, "PRIVATE": private}
    proc = subprocess.run(["bash", "-e", "-c", _check_step_script()], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert f"dual_stack={expected}" in out.read_text()
    if expected == "false":
        assert "::warning::" in proc.stdout
