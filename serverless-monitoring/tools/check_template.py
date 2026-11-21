#!/usr/bin/env python3
"""Structural checks for CloudFormation/SAM templates (YAML or JSON). A lightweight stand-in for `cfn-lint`
(CI runs the real cfn-lint too). Catches the mistakes that fail a deploy late: dangling references, unknown conditions,
Lambda handlers that do not exist, missing environment variables, malformed dashboard JSON.

    python tools/check_template.py template.yaml generated/alarms.template.json
"""
import ast
import json
import os
import re
import sys

import yaml

PSEUDO = {"AWS::Region", "AWS::AccountId", "AWS::Partition", "AWS::StackName", "AWS::StackId", "AWS::NoValue",
          "AWS::URLSuffix", "AWS::NotificationARNs"}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# environment variables each handler reads with os.environ[...] (a missing one is a crash at cold start)
REQUIRED_ENV = {"handlers.alert_router.handler": ["INCIDENT_TABLE", "ALERT_TOPIC_ARN"]}
EXPECTED_ENV = {"handlers.alert_router.handler": ["LOG_INGEST_LOG_GROUP", "API_INGEST_LOG_GROUP", "DASHBOARD_NAME"],
                "handlers.log_ingest.handler": ["IGNORE_LOG_GROUP_PREFIXES"], "handlers.api_ingest.handler": ["IGNORE_LOG_GROUP_PREFIXES"],
                "handlers.health_probe.handler": ["PROBE_TARGETS"]}


class _Loader(yaml.SafeLoader):
    pass


def _tag(loader, suffix, node):
    key = suffix if suffix in ("Ref", "Condition") else f"Fn::{suffix}"
    if isinstance(node, yaml.ScalarNode):
        return {key: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {key: loader.construct_sequence(node, deep=True)}
    return {key: loader.construct_mapping(node, deep=True)}


_Loader.add_multi_constructor("!", _tag)


def load(path):
    with open(path) as f:
        return yaml.load(f, Loader=_Loader) if path.endswith((".yaml", ".yml")) else json.load(f)


def _walk(node, visit, path=""):
    visit(node, path)
    if isinstance(node, dict):
        for k, v in node.items():
            _walk(v, visit, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _walk(v, visit, f"{path}[{i}]")


def _sub_vars(text):
    return [m for m in re.findall(r"\$\{([^}]+)\}", text) if not m.startswith("!")]


def check(template, base_dir=ROOT):
    problems = []
    resources, params = template.get("Resources", {}), template.get("Parameters", {})
    conditions, outputs = template.get("Conditions", {}), template.get("Outputs", {})
    known = set(resources) | set(params) | PSEUDO
    used_params = set()

    def check_ref(name, where):
        if name not in known:
            problems.append(f"{where}: Ref to unknown '{name}'")
        if name in params:
            used_params.add(name)

    def visit(node, path):
        if not isinstance(node, dict):
            return
        if "Ref" in node and len(node) == 1:
            check_ref(node["Ref"], path)
        if "Fn::GetAtt" in node:
            v = node["Fn::GetAtt"]
            target = (v.split(".")[0] if isinstance(v, str) else v[0])
            if target not in resources:
                problems.append(f"{path}: GetAtt on unknown resource '{target}'")
        if "Fn::Sub" in node:
            v = node["Fn::Sub"]
            text, local = (v, {}) if isinstance(v, str) else (v[0], v[1] if len(v) > 1 else {})
            for var in _sub_vars(text):
                base = var.split(".")[0]
                if base not in local:
                    check_ref(base, f"{path} (Sub)") if base in known else problems.append(f"{path}: Sub variable '${{{var}}}' is not defined")
        if "Fn::If" in node:
            cond = node["Fn::If"][0]
            if cond not in conditions:
                problems.append(f"{path}: Fn::If uses unknown condition '{cond}'")
        if "Condition" in node and len(node) == 1 and node["Condition"] not in conditions:
            problems.append(f"{path}: unknown condition '{node['Condition']}'")

    _walk(template, visit)
    for name, res in resources.items():
        for dep in ([res["DependsOn"]] if isinstance(res.get("DependsOn"), str) else res.get("DependsOn", [])):
            if dep not in resources:
                problems.append(f"Resources/{name}: DependsOn unknown '{dep}'")
        if isinstance(res.get("Condition"), str) and res["Condition"] not in conditions:
            problems.append(f"Resources/{name}: unknown condition '{res['Condition']}'")
        if "Type" not in res:
            problems.append(f"Resources/{name}: missing Type")
    for name, out in outputs.items():
        if isinstance(out.get("Condition"), str) and out["Condition"] not in conditions:
            problems.append(f"Outputs/{name}: unknown condition '{out['Condition']}'")
    for p in sorted(set(params) - used_params):
        problems.append(f"Parameters/{p}: declared but never used")

    problems += _check_functions(template, base_dir)
    problems += _check_alarms(resources)
    problems += _check_dashboards(resources)
    return problems


def _check_functions(template, base_dir):
    problems = []
    glob_env = template.get("Globals", {}).get("Function", {}).get("Environment", {}).get("Variables", {})
    for name, res in template.get("Resources", {}).items():
        if res.get("Type") != "AWS::Serverless::Function":
            continue
        props = res.get("Properties", {})
        handler = props.get("Handler", "")
        module, _, func = handler.rpartition(".")
        file = os.path.join(base_dir, template.get("Globals", {}).get("Function", {}).get("CodeUri", "src/"), *module.split(".")) + ".py"
        if not os.path.exists(file):
            problems.append(f"Resources/{name}: handler file {file} not found")
            continue
        with open(file) as source:
            tree = ast.parse(source.read())
        if not any(isinstance(n, ast.FunctionDef) and n.name == func for n in tree.body):
            problems.append(f"Resources/{name}: {file} has no function '{func}'")
        env = set(glob_env) | set(props.get("Environment", {}).get("Variables", {}))
        for var in REQUIRED_ENV.get(handler, []):
            if var not in env:
                problems.append(f"Resources/{name}: handler needs environment variable {var}")
        for var in EXPECTED_ENV.get(handler, []):
            if var not in env:
                problems.append(f"Resources/{name}: environment variable {var} is expected by {handler}")
    return problems


def _check_alarms(resources):
    problems = []
    for name, res in resources.items():
        if res.get("Type") != "AWS::CloudWatch::Alarm":
            continue
        p = res["Properties"]
        where = f"Resources/{name}"
        for key in ("ComparisonOperator", "EvaluationPeriods", "Threshold"):
            if key not in p:
                problems.append(f"{where}: missing {key}")
        if "DatapointsToAlarm" in p and isinstance(p["DatapointsToAlarm"], int) and p["DatapointsToAlarm"] > p.get("EvaluationPeriods", 0):
            problems.append(f"{where}: DatapointsToAlarm exceeds EvaluationPeriods")
        if len(str(p.get("AlarmName", ""))) > 255 or len(str(p.get("AlarmDescription", ""))) > 1024:
            problems.append(f"{where}: alarm name/description too long")
        if "Metrics" in p:
            ids = [m["Id"] for m in p["Metrics"]]
            if len(ids) != len(set(ids)):
                problems.append(f"{where}: duplicate metric ids")
            if sum(1 for m in p["Metrics"] if m.get("ReturnData", True) is True) != 1:
                problems.append(f"{where}: exactly one metric/expression must have ReturnData true")
            for m in p["Metrics"]:
                if "Expression" in m:
                    for ref in re.findall(r"\b[a-z][a-z0-9_]*\b", re.sub(r"\b(IF|SUM|METRICS|AVG|MAX|MIN)\b", "", m["Expression"])):
                        if ref not in ids and not ref.isdigit():
                            problems.append(f"{where}: expression uses undefined id '{ref}'")
        else:
            if not all(k in p for k in ("Namespace", "MetricName", "Period")):
                problems.append(f"{where}: single-metric alarm needs Namespace, MetricName and Period")
            if ("Statistic" in p) == ("ExtendedStatistic" in p):
                problems.append(f"{where}: exactly one of Statistic / ExtendedStatistic is required")
            if "ExtendedStatistic" in p and not re.fullmatch(r"p\d+(\.\d+)?", p["ExtendedStatistic"]):
                problems.append(f"{where}: bad ExtendedStatistic")
    return problems


def _dashboard_text(body):
    if isinstance(body, str):
        return body
    if isinstance(body, dict) and "Fn::Sub" in body:
        v = body["Fn::Sub"]
        return v if isinstance(v, str) else v[0]
    return None


def _check_dashboards(resources):
    problems = []
    for name, res in resources.items():
        if res.get("Type") != "AWS::CloudWatch::Dashboard":
            continue
        text = _dashboard_text(res["Properties"].get("DashboardBody"))
        if text is None:
            continue
        text = re.sub(r"\$\{[^}]+\}", "x", text)
        try:
            body = json.loads(text)
        except ValueError as exc:
            problems.append(f"Resources/{name}: DashboardBody is not valid JSON ({exc})")
            continue
        if len(text) > 100_000:
            problems.append(f"Resources/{name}: DashboardBody exceeds 100 KB")
        for i, w in enumerate(body.get("widgets", [])):
            if not all(k in w for k in ("type", "x", "y", "width", "height", "properties")):
                problems.append(f"Resources/{name}: widget {i} is missing a required field")
            elif w["x"] + w["width"] > 24:
                problems.append(f"Resources/{name}: widget {i} is wider than the 24-column grid")
    return problems


def main(paths):
    failed = False
    for path in paths:
        problems = check(load(path))
        print(f"{path}: {'OK' if not problems else str(len(problems)) + ' problem(s)'}")
        for p in problems:
            print("  -", p)
        failed = failed or bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["template.yaml"]))
