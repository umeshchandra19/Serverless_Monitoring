#!/usr/bin/env python3
"""Generate a CloudFormation template with CloudWatch alarms + one overview dashboard for YOUR services.

    python scripts/generate_alarms.py config/monitoring.json > generated/alarms.template.json
    aws cloudformation deploy --template-file generated/alarms.template.json --stack-name monitoring-alarms

Alarm names start with "<project>-svc-" which is exactly what the alert-router's EventBridge rule listens for, so every
generated alarm is automatically routed, enriched with context and tracked as an incident.
"""
import json
import re
import sys

NS_LOGS, NS_API, NS_PROBES = "Monitoring/Logs", "Monitoring/Api", "Monitoring/Probes"
REGION = "${AWS::Region}"


def logical_id(prefix, name):
    return prefix + re.sub(r"[^A-Za-z0-9]", "", "".join(p.capitalize() for p in re.split(r"[^A-Za-z0-9]+", name)))[:200]


def alarm_name(project, kind, name):
    return f"{project}-svc-{kind}-{re.sub(r'[^A-Za-z0-9._-]', '-', name)}"[:255]


def description(severity, runbook, text):
    parts = [f"severity={severity}"]
    if runbook:
        parts.append(f"runbook={runbook}")
    parts.append(text)
    return "; ".join(parts)[:1024]


def _metric(ns, name, dims, stat=None, period=60, ext=None):
    metric = {"Namespace": ns, "MetricName": name, "Dimensions": [{"Name": k, "Value": v} for k, v in dims.items()]}
    return {"Metric": metric, "Period": period, "Stat": stat or "Sum"}


def service_alarms(project, svc):
    sev = svc.get("severity", "P2")
    name = svc["name"]
    return {logical_id("AlarmErrors", name): {"Type": "AWS::CloudWatch::Alarm", "Properties": {
        "AlarmName": alarm_name(project, "errors", name),
        "AlarmDescription": description(sev, svc.get("runbook"), f"Error log volume for {name} is above {svc.get('errors_per_5min', 10)} per 5 minutes"),
        "Namespace": NS_LOGS, "MetricName": "ErrorCount", "Dimensions": [{"Name": "Service", "Value": name}],
        "Statistic": "Sum", "Period": 300, "EvaluationPeriods": 3, "DatapointsToAlarm": 2,
        "Threshold": svc.get("errors_per_5min", 10), "ComparisonOperator": "GreaterThanThreshold",
        "TreatMissingData": "notBreaching"}}}


def api_alarms(project, api):
    sev, name = api.get("severity", "P1"), api["name"]
    min_req, threshold = api.get("min_requests_per_min", 20), api.get("error_rate_percent", 5)
    out = {}
    dims = [{"Name": "Api", "Value": name}]

    def md(metric):
        return {"Namespace": NS_API, "MetricName": metric, "Dimensions": dims}
    out[logical_id("AlarmApi5xx", name)] = {"Type": "AWS::CloudWatch::Alarm", "Properties": {
        "AlarmName": alarm_name(project, "api5xx", name),
        "AlarmDescription": description(sev, api.get("runbook"), f"5xx rate of {name} above {threshold}% (ignored below {min_req} requests/min)"),
        "Metrics": [
            {"Id": "rate", "Expression": f"IF(requests < {min_req}, 0, 100 * errors / requests)", "Label": "5xx %", "ReturnData": True},
            {"Id": "errors", "MetricStat": {"Metric": md("Http5xx"), "Period": 60, "Stat": "Sum"}, "ReturnData": False},
            {"Id": "requests", "MetricStat": {"Metric": md("RequestCount"), "Period": 60, "Stat": "Sum"}, "ReturnData": False}],
        "EvaluationPeriods": 5, "DatapointsToAlarm": 3, "Threshold": threshold,
        "ComparisonOperator": "GreaterThanThreshold", "TreatMissingData": "notBreaching"}}
    if api.get("p95_latency_ms"):
        out[logical_id("AlarmApiLatency", name)] = {"Type": "AWS::CloudWatch::Alarm", "Properties": {
            "AlarmName": alarm_name(project, "apilatency", name),
            "AlarmDescription": description(api.get("latency_severity", "P2"), api.get("runbook"), f"p95 latency of {name} above {api['p95_latency_ms']} ms"),
            "Namespace": NS_API, "MetricName": "Latency", "Dimensions": dims, "ExtendedStatistic": "p95", "Period": 60,
            "EvaluationPeriods": 5, "DatapointsToAlarm": 3, "Threshold": api["p95_latency_ms"],
            "ComparisonOperator": "GreaterThanThreshold", "TreatMissingData": "notBreaching"}}
    return out


def probe_alarms(project, probe):
    sev, name = probe.get("severity", "P1"), probe["name"]
    dims = [{"Name": "Target", "Value": name}]
    out = {logical_id("AlarmProbe", name): {"Type": "AWS::CloudWatch::Alarm", "Properties": {
        "AlarmName": alarm_name(project, "probe", name),
        "AlarmDescription": description(sev, probe.get("runbook"), f"Health probe {name} failing for 3 consecutive minutes"),
        "Namespace": NS_PROBES, "MetricName": "Availability", "Dimensions": dims, "Statistic": "Minimum", "Period": 60,
        "EvaluationPeriods": 3, "DatapointsToAlarm": 3, "Threshold": 1, "ComparisonOperator": "LessThanThreshold",
        "TreatMissingData": "breaching"}}}   # no data = the probe itself stopped = also worth a page
    if probe.get("cert_days"):
        out[logical_id("AlarmCert", name)] = {"Type": "AWS::CloudWatch::Alarm", "Properties": {
            "AlarmName": alarm_name(project, "cert", name),
            "AlarmDescription": description("P3", probe.get("runbook"), f"TLS certificate of {name} expires in less than {probe['cert_days']} days"),
            "Namespace": NS_PROBES, "MetricName": "CertDaysRemaining", "Dimensions": dims, "Statistic": "Minimum", "Period": 3600,
            "EvaluationPeriods": 1, "Threshold": probe["cert_days"], "ComparisonOperator": "LessThanThreshold",
            "TreatMissingData": "notBreaching"}}
    return out


# ---------------- dashboard ----------------

def _widget(x, y, w, h, title, metrics, stat="Sum", period=60, **extra):
    props = {"metrics": metrics, "view": "timeSeries", "stacked": False, "region": REGION, "title": title, "period": period, "stat": stat}
    props.update(extra)
    return {"type": "metric", "x": x, "y": y, "width": w, "height": h, "properties": props}


def dashboard_body(cfg, alarm_names):
    project, widgets, y = cfg["project"], [], 0

    def text(markdown):
        nonlocal y
        widgets.append({"type": "text", "x": 0, "y": y, "width": 24, "height": 1, "properties": {"markdown": markdown}})
        y += 1

    for api in cfg.get("apis", []):
        n = api["name"]
        text(f"## API: {n}")
        d = ["Api", n]
        widgets.append(_widget(0, y, 8, 6, "Requests / 4xx / 5xx", [[NS_API, "RequestCount", *d], [".", "Http4xx", *d], [".", "Http5xx", *d]]))
        widgets.append(_widget(8, y, 8, 6, "5xx rate %", [
            [{"expression": "IF(requests < 5, 0, 100 * errors / requests)", "label": "5xx %", "id": "rate"}],
            [NS_API, "Http5xx", *d, {"id": "errors", "visible": False}], [NS_API, "RequestCount", *d, {"id": "requests", "visible": False}]],
            yAxis={"left": {"min": 0}}))
        widgets.append(_widget(16, y, 8, 6, "Latency (ms)", [[NS_API, "Latency", *d, {"stat": "p50", "label": "p50"}],
                                                          [".", "Latency", *d, {"stat": "p95", "label": "p95"}],
                                                          [".", "Latency", *d, {"stat": "p99", "label": "p99"}]]))
        y += 6
    for svc in cfg.get("services", []):
        n = svc["name"]
        text(f"## Service: {n}")
        d = ["Service", n]
        widgets.append(_widget(0, y, 12, 6, "Errors / warnings / critical", [[NS_LOGS, "ErrorCount", *d], [".", "WarnCount", *d], [".", "CriticalCount", *d]]))
        widgets.append(_widget(12, y, 12, 6, "Request latency (ms)", [[NS_LOGS, "RequestLatencyMs", *d, {"stat": "p50"}],
                                                                  [".", "RequestLatencyMs", *d, {"stat": "p95"}]]))
        y += 6
    probes = cfg.get("probes", [])
    if probes:
        text("## Synthetic probes")
        widgets.append(_widget(0, y, 8, 6, "Availability (1 = up)", [[NS_PROBES, "Availability", "Target", p["name"]] for p in probes], stat="Minimum", yAxis={"left": {"min": 0, "max": 1.1}}))
        widgets.append(_widget(8, y, 8, 6, "Response time (ms)", [[NS_PROBES, "ResponseTimeMs", "Target", p["name"]] for p in probes], stat="Average"))
        widgets.append(_widget(16, y, 8, 6, "Certificate days remaining", [[NS_PROBES, "CertDaysRemaining", "Target", p["name"]] for p in probes if p.get("cert_days")], stat="Minimum", period=3600))
        y += 6
    text("## Alerts and incidents")
    arns = [f"arn:aws:cloudwatch:{REGION}:${{AWS::AccountId}}:alarm:{a}" for a in alarm_names]
    widgets.append({"type": "alarm", "x": 0, "y": y, "width": 12, "height": 6, "properties": {"title": "Alarm status", "alarms": arns}})
    group = cfg.get("log_ingest_log_group", f"/aws/lambda/{project}-log-ingest")
    widgets.append({"type": "log", "x": 12, "y": y, "width": 12, "height": 6, "properties": {
        "region": REGION, "title": "Most recent error signatures", "view": "table",
        "query": f"SOURCE '{group}' | fields @timestamp, service, count, sample | filter type = 'error_signature' | sort @timestamp desc | limit 20"}})
    return {"widgets": widgets}


def _merge(resources, new):
    clash = set(resources) & set(new)
    if clash:
        raise ValueError(f"duplicate names in the config (same alarm defined twice): {sorted(clash)}")
    resources.update(new)


def generate(cfg):
    project = cfg.get("project", "monitoring")
    cfg = dict(cfg, project=project)
    resources = {}
    for s in cfg.get("services", []):
        _merge(resources, service_alarms(project, s))
    for a in cfg.get("apis", []):
        _merge(resources, api_alarms(project, a))
    for p in cfg.get("probes", []):
        _merge(resources, probe_alarms(project, p))
    names = [r["Properties"]["AlarmName"] for r in resources.values()]
    if len(set(names)) != len(names):
        raise ValueError("duplicate alarm names: service/api/probe names must be unique")
    dash_name = cfg.get("dashboard_name", f"{project}-overview")
    resources["OverviewDashboard"] = {"Type": "AWS::CloudWatch::Dashboard", "Properties": {
        "DashboardName": dash_name, "DashboardBody": {"Fn::Sub": json.dumps(dashboard_body(cfg, names), separators=(",", ":"))}}}
    return {"AWSTemplateFormatVersion": "2010-09-09",
            "Description": f"Generated CloudWatch alarms and dashboard for {project} (scripts/generate_alarms.py)",
            "Resources": resources,
            "Outputs": {"DashboardUrl": {"Value": {"Fn::Sub": f"https://${{AWS::Region}}.console.aws.amazon.com/cloudwatch/home?region=${{AWS::Region}}#dashboards:name={dash_name}"}}}}


def main(argv):
    if len(argv) != 2:
        sys.exit("usage: generate_alarms.py CONFIG.json > alarms.template.json")
    with open(argv[1]) as f:
        print(json.dumps(generate(json.load(f)), indent=2))


if __name__ == "__main__":
    main(sys.argv)
