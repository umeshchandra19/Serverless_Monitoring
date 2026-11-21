"""Infrastructure-as-code checks that need no AWS access."""
import copy
import io
import json
import os
import re
import sys
import unittest

from tests.helpers import Capture, T0_MS, alarm_event, logs_event
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import check_template as ct  # noqa: E402
import generate_alarms as ga  # noqa: E402

CONFIG = json.load(open(os.path.join(ROOT, "config", "monitoring.example.json")))


class TemplateCheckerTests(unittest.TestCase):
    def setUp(self):
        self.t = ct.load(os.path.join(ROOT, "template.yaml"))

    def test_real_template_is_clean(self):
        self.assertEqual(ct.check(self.t), [])

    def test_generated_template_is_clean(self):
        self.assertEqual(ct.check(ga.generate(CONFIG)), [])

    def problems(self, mutate):
        t = copy.deepcopy(self.t)
        mutate(t)
        return ct.check(t)

    def test_detects_dangling_reference(self):
        p = self.problems(lambda t: t["Resources"]["AlertTopic"]["Properties"].update(DisplayName={"Ref": "NopeTable"}))
        self.assertTrue(any("unknown 'NopeTable'" in x for x in p))

    def test_detects_unknown_getatt_target_and_sub_variable(self):
        p = self.problems(lambda t: t["Outputs"].update(X={"Value": {"Fn::GetAtt": "Ghost.Arn"}}, Y={"Value": {"Fn::Sub": "${Missing}"}}))
        self.assertTrue(any("GetAtt on unknown resource 'Ghost'" in x for x in p))
        self.assertTrue(any("'${Missing}' is not defined" in x for x in p))

    def test_detects_bad_dependson_and_condition(self):
        p = self.problems(lambda t: t["Resources"]["LogIngestFunction"].update(DependsOn="Ghost", Condition="NoSuchCondition"))
        self.assertTrue(any("DependsOn unknown 'Ghost'" in x for x in p))
        self.assertTrue(any("unknown condition 'NoSuchCondition'" in x for x in p))

    def test_detects_missing_handler_and_missing_env(self):
        def mutate(t):
            t["Resources"]["LogIngestFunction"]["Properties"]["Handler"] = "handlers.log_ingest.nope"
            t["Resources"]["AlertRouterFunction"]["Properties"]["Environment"]["Variables"].pop("INCIDENT_TABLE")
        p = self.problems(mutate)
        self.assertTrue(any("has no function 'nope'" in x for x in p))
        self.assertTrue(any("needs environment variable INCIDENT_TABLE" in x for x in p))

    def test_detects_broken_dashboard_json_and_overwide_widget(self):
        def mutate(t):
            t["Resources"]["PipelineDashboard"]["Properties"]["DashboardBody"] = {"Fn::Sub": '{"widgets":[{"type":"text",}'}
        self.assertTrue(any("not valid JSON" in x for x in self.problems(mutate)))

        def wide(t):
            t["Resources"]["PipelineDashboard"]["Properties"]["DashboardBody"] = {"Fn::Sub": json.dumps(
                {"widgets": [{"type": "text", "x": 20, "y": 0, "width": 12, "height": 1, "properties": {}}]})}
        self.assertTrue(any("24-column" in x for x in self.problems(wide)))

    def test_detects_unused_parameter_and_bad_alarm(self):
        def mutate(t):
            t["Parameters"]["Orphan"] = {"Type": "String"}
            a = t["Resources"]["FailedEventsAlarm"]["Properties"]
            a["DatapointsToAlarm"] = 9
            a.pop("Statistic")
        p = self.problems(mutate)
        self.assertTrue(any("Parameters/Orphan" in x for x in p))
        self.assertTrue(any("DatapointsToAlarm exceeds" in x for x in p))
        self.assertTrue(any("Statistic / ExtendedStatistic" in x for x in p))


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.t = ga.generate(CONFIG)
        self.alarms = {k: v["Properties"] for k, v in self.t["Resources"].items() if v["Type"] == "AWS::CloudWatch::Alarm"}

    def test_every_alarm_name_matches_the_routers_event_rule(self):
        template = ct.load(os.path.join(ROOT, "template.yaml"))
        prefix = template["Resources"]["AlertRouterFunction"]["Properties"]["Events"]["AlarmStateChange"]["Properties"]["Pattern"]["detail"]["alarmName"][0]["prefix"]["Fn::Sub"]
        expected = prefix.replace("${Project}", CONFIG["project"])
        self.assertTrue(self.alarms)
        for p in self.alarms.values():
            self.assertTrue(p["AlarmName"].startswith(expected), p["AlarmName"])

    def test_descriptions_carry_severity_and_runbook_the_router_understands(self):
        from monitoring.alerts import parse_description
        sev, runbook, summary = parse_description(self.alarms["AlarmApi5xxMonitoringDemoOrdersApi"]["AlarmDescription"])
        self.assertEqual((sev, runbook), ("P1", "https://wiki.example.com/runbooks/api-5xx"))
        self.assertIn("5xx rate", summary)

    def test_thresholds_come_from_config(self):
        self.assertEqual(self.alarms["AlarmErrorsMonitoringDemoOrders"]["Threshold"], 10)
        self.assertEqual(self.alarms["AlarmApiLatencyMonitoringDemoOrdersApi"]["Threshold"], 1500)
        self.assertEqual(self.alarms["AlarmApiLatencyMonitoringDemoOrdersApi"]["ExtendedStatistic"], "p95")
        self.assertIn("< 20", self.alarms["AlarmApi5xxMonitoringDemoOrdersApi"]["Metrics"][0]["Expression"])

    def test_probe_alarm_treats_missing_data_as_breaching(self):
        self.assertEqual(self.alarms["AlarmProbeOrdersHealth"]["TreatMissingData"], "breaching")
        self.assertEqual(self.alarms["AlarmErrorsMonitoringDemoOrders"]["TreatMissingData"], "notBreaching")

    def test_dashboard_lists_every_generated_alarm(self):
        body = json.loads(self.t["Resources"]["OverviewDashboard"]["Properties"]["DashboardBody"]["Fn::Sub"])
        widget = next(w for w in body["widgets"] if w["type"] == "alarm")
        self.assertEqual(len(widget["properties"]["alarms"]), len(self.alarms))

    def test_duplicate_names_are_rejected(self):
        with self.assertRaises(ValueError):
            ga.generate({"project": "m", "services": [{"name": "a"}, {"name": "a"}]})

    def test_names_with_odd_characters_stay_valid(self):
        t = ga.generate({"project": "m", "services": [{"name": "my svc/prod (eu)"}]})
        [(lid, res)] = [(k, v) for k, v in t["Resources"].items() if v["Type"] == "AWS::CloudWatch::Alarm"]
        self.assertRegex(lid, r"^[A-Za-z0-9]+$")
        self.assertRegex(res["Properties"]["AlarmName"], r"^[A-Za-z0-9._-]+$")
        self.assertEqual(ct.check(t), [])


def emitted_catalog():
    """Every (namespace, metric, dimension-names) the code can emit, discovered by actually running it."""
    from handlers import api_ingest, health_probe, log_ingest
    from monitoring.router import RouterDeps, handle_event
    from monitoring.store import InMemoryStore
    from datetime import datetime, timezone
    cap = Capture(alert_patterns=[])
    msgs = ['{"level":"error","msg":"x","latency_ms":5}', '{"level":"warn","msg":"w"}', "{bad", "Task timed out after 3 seconds",
            "REPORT RequestId: a Duration: 5.0 ms Billed Duration: 5 ms Memory Size: 128 MB Max Memory Used: 50 MB Init Duration: 100.0 ms"]
    log_ingest.process(logs_event("/aws/lambda/svc", msgs), cap.deps)
    api = json.dumps({"requestId": "r", "httpMethod": "GET", "resourcePath": "/x", "status": "500", "responseLatency": "5000", "integrationLatency": "4", "traceId": "t"})
    api_ingest.process(logs_event("/aws/apigateway/api", [api, "nope"]), cap.deps)
    health_probe.process([{"name": "p", "url": "https://x.test/"}], emit=lambda r: cap.emitted.extend(r), fetch=lambda u, t: (200, 5.0, None),
                         cert_check=lambda h, p, t: 30.0, now_ms=T0_MS)
    now = datetime(2026, 11, 19, 10, 0, 7, tzinfo=timezone.utc)
    deps = RouterDeps(store=InMemoryStore(), publish=lambda s, m: None, now=lambda: now, emit=lambda r: cap.emitted.extend(r), log=lambda o: None)
    handle_event(alarm_event(), deps)
    handle_event(alarm_event(), deps)
    handle_event(alarm_event(state="OK", at="2026-11-19T10:05:00.000+0000"), deps)
    catalog = set()
    for r in cap.emitted:
        for block in r["_aws"]["CloudWatchMetrics"]:
            for dim_set in block["Dimensions"]:
                for m in block["Metrics"]:
                    catalog.add((block["Namespace"], m["Name"], tuple(sorted(dim_set))))
    return catalog


def dashboard_metric_refs(widgets):
    refs = []
    for w in widgets:
        if w.get("type") != "metric":
            continue
        prev = []
        for row in w["properties"]["metrics"]:
            row = [x for x in row if not isinstance(x, dict) or "expression" not in x]
            cells = [p if c == "." and i < len(prev) else c for i, c in enumerate(row) for p in [prev[i] if i < len(prev) else None]]
            cells = [c for c in cells if not isinstance(c, dict)]
            if len(cells) >= 2 and isinstance(cells[0], str):
                prev = cells
                refs.append((cells[0], cells[1], tuple(sorted(cells[2::2]))))
    return refs


class MetricContractTests(unittest.TestCase):
    """Alarms and dashboards may only reference metrics that the Lambda code really emits."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = emitted_catalog()

    def test_catalog_is_not_empty(self):
        self.assertGreater(len(self.catalog), 20)

    def test_generated_alarms_reference_emitted_metrics(self):
        for key, res in ga.generate(CONFIG)["Resources"].items():
            if res["Type"] != "AWS::CloudWatch::Alarm":
                continue
            p = res["Properties"]
            refs = [(p["Namespace"], p["MetricName"], tuple(sorted(d["Name"] for d in p["Dimensions"])))] if "Namespace" in p else \
                [(m["MetricStat"]["Metric"]["Namespace"], m["MetricStat"]["Metric"]["MetricName"], tuple(sorted(d["Name"] for d in m["MetricStat"]["Metric"]["Dimensions"])))
                 for m in p["Metrics"] if "MetricStat" in m]
            for ref in refs:
                self.assertIn(ref, self.catalog, f"{key} references a metric the code never emits: {ref}")

    def test_generated_dashboard_references_emitted_metrics(self):
        body = json.loads(ga.generate(CONFIG)["Resources"]["OverviewDashboard"]["Properties"]["DashboardBody"]["Fn::Sub"])
        refs = dashboard_metric_refs(body["widgets"])
        self.assertGreater(len(refs), 10)
        for ref in refs:
            self.assertIn(ref, self.catalog, f"dashboard references a metric the code never emits: {ref}")

    def test_pipeline_dashboard_search_expressions_match_emitted_metrics(self):
        template = ct.load(os.path.join(ROOT, "template.yaml"))
        text = template["Resources"]["PipelineDashboard"]["Properties"]["DashboardBody"]["Fn::Sub"]
        found = re.findall(r"\{(Monitoring/[A-Za-z]+),([A-Za-z]+)\} MetricName=\\\"([A-Za-z]+)\\\"", text)
        self.assertEqual(len(found), 4)
        for ns, dim, metric in found:
            self.assertIn((ns, metric, (dim,)), self.catalog)

    def test_alarm_metric_math_works_with_real_emitted_values(self):
        """The 5xx-rate expression, evaluated by hand on emitted numbers, gives the right percentage."""
        cap = Capture()
        lines = [json.dumps({"requestId": str(i), "httpMethod": "GET", "resourcePath": "/o", "status": "500" if i < 3 else "200",
                             "responseLatency": "10", "integrationLatency": "9", "traceId": "-"}) for i in range(30)]
        from handlers import api_ingest
        api_ingest.process(logs_event("/aws/apigateway/a", lines, [T0_MS] * 30), cap.deps)
        errors = cap.metric("Http5xx", without=("Route",), Api="a")[0][1]
        requests = cap.metric("RequestCount", without=("Route",), Api="a")[0][1]
        self.assertAlmostEqual(100 * errors / requests, 10.0)


if __name__ == "__main__":
    unittest.main()
