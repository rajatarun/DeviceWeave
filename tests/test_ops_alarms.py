"""Every DeviceWeave function's errors reach someone.

Most failures here are caught and logged (a provider failure is a 502, an
expired Ring token a logged auth error), so Lambda's Errors metric does not
see them. Each function's log group feeds one ErrorLines metric and one alarm
notifies the ops topic. A function added later must feed it too.
"""
from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]


class _Loader(yaml.SafeLoader):
    pass


def _tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {suffix: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {suffix: loader.construct_sequence(node, deep=True)}
    return {suffix: loader.construct_mapping(node, deep=True)}


_Loader.add_multi_constructor("!", _tag)
T = yaml.load((REPO / "template.yaml").read_text(), Loader=_Loader)  # noqa: S506
R = T["Resources"]


def _of_type(kind):
    return {k: v for k, v in R.items() if v["Type"] == kind}


def test_every_lambda_log_group_feeds_the_error_metric():
    lambda_groups = {k for k, v in _of_type("AWS::Logs::LogGroup").items()
                     if "/aws/lambda/" in str(v["Properties"]["LogGroupName"])}
    fed = {v["Properties"]["LogGroupName"]["Ref"]
           for v in _of_type("AWS::Logs::MetricFilter").values()
           if v["Properties"]["MetricTransformations"][0]["MetricName"] == "ErrorLines"}
    assert len(lambda_groups) >= 7 and lambda_groups <= fed, sorted(lambda_groups - fed)


def test_every_function_has_a_declared_log_group():
    functions = {v["Properties"]["FunctionName"]["Sub"]
                 for v in _of_type("AWS::Serverless::Function").values()}
    groups = {v["Properties"]["LogGroupName"]["Sub"]
              for v in _of_type("AWS::Logs::LogGroup").values()}
    assert {f"/aws/lambda/{f}" for f in functions} <= groups


def test_the_alarm_fires_on_the_first_error_line_and_notifies_the_topic():
    a = R["ErrorLinesAlarm"]["Properties"]
    assert (a["Namespace"], a["MetricName"]) == ("DeviceWeave/Ops", "ErrorLines")
    assert a["Threshold"] == 1 and a["TreatMissingData"] == "notBreaching"
    assert a["AlarmActions"] == [{"Ref": "OpsAlertsTopic"}]
    for f in _of_type("AWS::Logs::MetricFilter").values():
        for term in ('?"[ERROR]"', '?"Task timed out"', '?"Runtime.ExitError"'):
            assert term in f["Properties"]["FilterPattern"]


def test_email_is_optional_and_never_committed():
    assert T["Parameters"]["AlertEmail"]["Default"] == ""
    assert R["OpsAlertsEmailSubscription"]["Condition"] == "HasAlertEmail"
    workflow = (REPO / ".github/workflows/deploy.yml").read_text()
    assert "ALERT_EMAIL: ${{ vars.ALERT_EMAIL }}" in workflow and '"$ALERT_ARG"' in workflow
