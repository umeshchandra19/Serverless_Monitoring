import json
import unittest

from tests.helpers import Capture, T0_MS, logs_event
from handlers import log_ingest

MIN = 60_000


class LogIngestTests(unittest.TestCase):
    def run_ingest(self, messages, timestamps=None, group="/aws/lambda/orders-fn", **config):
        cap = Capture(**config)
        result = log_ingest.process(logs_event(group, messages, timestamps), cap.deps)
        return cap, result

    def test_counts_and_timestamps_are_per_minute(self):
        msgs = ['{"level":"error","msg":"a"}', '{"level":"error","msg":"b"}', '{"level":"info","msg":"c"}', '{"level":"warn","msg":"d"}']
        ts = [T0_MS, T0_MS + 5_000, T0_MS + MIN + 1, T0_MS + MIN + 2]
        cap, result = self.run_ingest(msgs, ts)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(cap.metric("ErrorCount", Service="orders-fn"), [(T0_MS, 2), (T0_MS + MIN, 0)])
        self.assertEqual(cap.metric("WarnCount", Service="orders-fn"), [(T0_MS, 0), (T0_MS + MIN, 1)])
        self.assertEqual(cap.metric("LogEvents"), [(T0_MS, 2), (T0_MS + MIN, 2)])

    def test_request_latency_keeps_its_distribution(self):
        msgs = ['{"level":"info","msg":"served","latency_ms":%d}' % v for v in (10, 20, 30)]
        cap, _ = self.run_ingest(msgs, [T0_MS] * 3)
        self.assertEqual(cap.metric("RequestLatencyMs"), [(T0_MS, [10.0, 20.0, 30.0])])

    def test_lambda_report_lines_become_duration_and_memory_metrics(self):
        msg = "REPORT RequestId: a Duration: 100.00 ms Billed Duration: 100 ms Memory Size: 128 MB Max Memory Used: 90 MB"
        cap, _ = self.run_ingest([msg], [T0_MS])
        self.assertEqual(cap.metric("LambdaDurationMs"), [(T0_MS, [100.0])])  # a distribution, even with one sample
        self.assertEqual(cap.metric("LambdaMaxMemoryMb"), [(T0_MS, 90.0)])

    def test_service_name_comes_from_log_group_or_override(self):
        cap, _ = self.run_ingest(['{"level":"info","msg":"x"}'], group="/ecs/payments")
        self.assertEqual(cap.metric("LogEvents", Service="payments")[0][1], 1)
        cap, _ = self.run_ingest(['{"level":"info","msg":"x"}'], group="/ecs/payments", service_map={"/ecs/payments": "billing"})
        self.assertTrue(cap.metric("LogEvents", Service="billing"))

    def test_error_signatures_are_logged_for_alert_enrichment(self):
        msgs = ['{"level":"error","msg":"db timeout order %d","request_id":"r%d"}' % (i, i) for i in range(4)]
        cap, _ = self.run_ingest(msgs)
        sigs = [l for l in cap.logs if l.get("type") == "error_signature"]
        self.assertEqual(len(sigs), 1)
        self.assertEqual((sigs[0]["count"], sigs[0]["service"]), (4, "orders-fn"))

    def test_critical_logs_publish_one_event_per_distinct_problem(self):
        msgs = ["2026 abc Task timed out after 3.00 seconds"] * 3 + ['{"level":"fatal","msg":"disk gone"}', '{"level":"error","msg":"ordinary"}']
        cap, result = self.run_ingest(msgs)
        details = [json.loads(e["Detail"]) for e in cap.events]
        self.assertEqual(len(details), 2)
        self.assertEqual({d["reason"] for d in details}, {"timeout after 3.00s", "critical severity"})
        self.assertEqual(max(d["count"] for d in details), 3)
        self.assertEqual({e["DetailType"] for e in cap.events}, {"LogAlert"})
        self.assertEqual(result["alerts_published"], 2)

    def test_custom_alert_patterns(self):
        cap, _ = self.run_ingest(['{"level":"error","msg":"payment gateway refused card"}'], alert_patterns=["gateway refused"])
        self.assertEqual(len(cap.events), 1)
        self.assertIn("matched", json.loads(cap.events[0]["Detail"])["reason"])

    def test_alert_storm_guard_limits_events_but_not_metrics(self):
        msgs = ['{"level":"fatal","msg":"unique problem %s"}' % ("x" * i) for i in range(1, 40)]
        cap, _ = self.run_ingest(msgs, max_alerts_per_invocation=5)
        self.assertEqual(len(cap.events), 5)
        self.assertEqual(cap.metric("CriticalCount")[0][1], 39)

    def test_losing_the_alert_does_not_lose_the_metrics(self):
        cap = Capture()
        cap.fail_put_events = True
        result = log_ingest.process(logs_event("/aws/lambda/x", ['{"level":"fatal","msg":"boom"}']), cap.deps)
        self.assertEqual(result["alerts_failed"], 1)
        self.assertTrue(cap.metric("CriticalCount"))

    def test_pipeline_never_processes_its_own_log_groups(self):
        cap, result = self.run_ingest(['{"level":"error","msg":"x"}'], group="/aws/lambda/monitoring-log-ingest",
                                      ignore_prefixes=["/aws/lambda/monitoring-"])
        self.assertEqual(result["status"], "ignored_own_log_group")
        self.assertEqual(cap.emitted, [])

    def test_own_output_lines_are_skipped(self):
        own = ['{"_aws":{"Timestamp":1},"ErrorCount":9}', '{"type":"error_signature","service":"x","count":3}']
        cap, _ = self.run_ingest(own + ['{"level":"info","msg":"real"}'])
        self.assertEqual(cap.metric("LogEvents")[0][1], 1)

    def test_bad_lines_are_counted_not_fatal(self):
        cap, result = self.run_ingest(["{broken", '{"level":"info","msg":"fine"}'])
        self.assertEqual(cap.metric("ParseFailures")[0][1], 1)
        self.assertEqual(result["status"], "ok")

    def test_control_messages_and_garbage_events_are_ignored(self):
        cap = Capture()
        self.assertEqual(log_ingest.process({"awslogs": {"data": "!!!"}}, cap.deps)["status"], "ignored")
        self.assertEqual(log_ingest.process({}, cap.deps)["status"], "ignored")
        self.assertEqual(cap.emitted, [])


if __name__ == "__main__":
    unittest.main()
