"""Every function the template deploys loads the way Lambda loads it.

A handler module that raises at import fails every invocation before any code
under test runs, and a unit test that imports the pieces from the repository
root, with the developer's environment, does not notice. ContextWeave lost
/mcp, /health and /finance for three hours that way on 2026-10-22: LOG_LEVEL
was set to a custom level the template uses, and five handlers passed it to
logging.setLevel at import. This test starts a fresh interpreter per function
with the function's CodeUri as the top-level directory and only the
environment the template gives it (a parameter reference takes the
parameter's default; any other value computed at deploy time becomes a
placeholder: what is under test is the import, not the value), and imports
the handler.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "template.yaml"


class _Loader(yaml.SafeLoader):
    pass


def _intrinsic(loader, suffix, node):
    if suffix == "Ref" and isinstance(node, yaml.ScalarNode):
        return {"Ref": loader.construct_scalar(node)}
    return None


_Loader.add_multi_constructor("!", _intrinsic)
T = yaml.load(TEMPLATE.read_text(), Loader=_Loader)  # noqa: S506
GLOBALS = (T.get("Globals") or {}).get("Function") or {}
PARAMETERS = T.get("Parameters") or {}


def _value(v):
    """A template value as the function would see it: a parameter's default, else a placeholder."""
    if isinstance(v, dict) and "Ref" in v:
        default = (PARAMETERS.get(v["Ref"]) or {}).get("Default")
        return "placeholder" if default is None else str(default)
    return "placeholder" if v is None or isinstance(v, (dict, list)) else str(v)


def _deployed():
    for name, res in T["Resources"].items():
        if res.get("Type") != "AWS::Serverless::Function":
            continue
        props = res["Properties"]
        runtime = props.get("Runtime") or GLOBALS.get("Runtime") or ""
        code_uri = props.get("CodeUri") or GLOBALS.get("CodeUri")
        if not str(runtime).startswith("python") or not isinstance(code_uri, str):
            continue  # inline code or another runtime
        module, _, attr = props["Handler"].rpartition(".")
        env = {
            **((GLOBALS.get("Environment") or {}).get("Variables") or {}),
            **((props.get("Environment") or {}).get("Variables") or {}),
        }
        env = {k: _value(v) for k, v in env.items()}
        yield pytest.param(TEMPLATE.parent / code_uri, module.replace("/", "."), attr, env, id=name)


DEPLOYED = list(_deployed())


def test_the_template_deploys_python_functions():
    assert DEPLOYED


@pytest.mark.parametrize("code_dir,module,attr,template_env", DEPLOYED)
def test_the_handler_imports_with_the_templates_environment(code_dir, module, attr, template_env):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "PYTHON"))}
    env.update(template_env)
    env.update(AWS_DEFAULT_REGION="us-east-1", AWS_REGION="us-east-1", PYTHONDONTWRITEBYTECODE="1")
    code = (
        "import importlib; "
        f"m = importlib.import_module({module!r}); "
        f"assert callable(getattr(m, {attr!r})), 'handler {attr} is not callable'"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=code_dir, env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
