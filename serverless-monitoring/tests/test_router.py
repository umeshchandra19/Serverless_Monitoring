import json
import unittest
from datetime import datetime, timedelta, timezone

from tests.helpers import T0, alarm_event
from monitoring import alerts as al
from monitoring.router import RouterDeps, handle_event
from monitoring.store import InMemoryStore


class Harness:
    def __init__(self, **config):
        self.now = T0 + timedelta(seconds=7)
        self.sent, self.hooks, self.emitted, self.logs, self.store = [], [], [], [], InMemoryStore()
        self.fail_publish = 0
        self.find_logs_calls = []
        self.log_lines = {}
        cfg = {"region": "ap-south-1", "dashboard_name": "monitoring-overview", "log_ingest_group": "/aws/lambda/monitoring-log-ingest",
               "api_ingest_group": "/aws/lambda/monitoring-api-ingest"}
        cfg.update(config)
        self.deps = RouterDeps(store=self.store, publish=self._publish, now=lambda: self.now, find_logs=self._find,
                               emit=lambda r: self.emitted.extend(r), log=self.logs.append, config=cfg)

    def _publish(self, subject, message):
        if self.fail_publish:
            self.fail_publish -= 1
            raise RuntimeError("sns down")
        self.sent.append((subject, message))

    def _find(self, group, pattern, start_ms, limit):
        self.find_logs_calls.append((group, pattern))
        return self.log_lines.get("signatures" if "error_signature" in pattern else "exemplars", [])

    def fire(self, event):
        return handle_event(event, self.deps)

    def metric(self, name):
        return [r[name] for r in self.emitted if name in r]


def at(seconds):
    return (T0 + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


class ParsingTests(unittest.TestCase):
    def test_description_conventions(self):
        self.assertEqual(al.parse_description("severity=P1; runbook=https://wiki/x; API is failing"), ("P1", "https://wiki/x", "API is failing"))
        self.assertEqual(al.parse_description("just words"), ("P3", None, "just words"))
        self.assertEqual(al.parse_description("")[0], "P3")
        self.assertEqual(al.parse_description("Severity = p2")[0], "P2")

    def test_alarm_event_parsing(self):
        a = al.parse_alarm_event(alarm_event(), T0)
        self.assertEqual((a.name, a.state, a.severity, a.api, a.runbook), ("monitoring-svc-api5xx-orders", "ALARM", "P1", "orders", "https://wiki.example/runbooks/5xx"))
        self.assertEqual(a.occurred_at, T0)
        self.assertEqual(a.region, "ap-south-1")

    def test_time_parsing_fallbacks(self):
        self.assertEqual(al.parse_time("2026-11-19T10:00:05Z", None), T0 + timedelta(seconds=5))
        self.assertEqual(al.parse_time("garbage", T0), T0)
        self.assertEqual(al.parse_time(None, T0), T0)


class AlarmLifecycleTests(unittest.TestCase):
    def test_new_alarm_pages_with_context_and_records_an_incident(self):
        h = Harness()
        h.log_lines["signatures"] = [json.dumps({"type": "error_signature", "fingerprint": "f1", "count": 7, "sample": "db timeout for order 1"}),
                                     json.dumps({"type": "error_signature", "fingerprint": "f1", "count": 5, "sample": "db timeout for order 2"}),
                                     json.dumps({"type": "error_signature", "fingerprint": "f2", "count": 1, "sample": "other"})]
        h.log_lines["exemplars"] = [json.dumps({"type": "api_exemplar", "reason": "slow", "route": "GET /a", "status": 200, "latency_ms": 4000, "trace_id": "1-aa"}),
                                    json.dumps({"type": "api_exemplar", "reason": "5xx", "route": "POST /orders", "status": 502, "latency_ms": 900, "trace_id": "1-bb"})]
        result = h.fire(alarm_event(dims={"Api": "orders", "Service": "orders-fn"}))
        self.assertEqual(result["action"], "new")
        self.assertEqual(result["notify_latency_seconds"], 7)
        [(subject, text)] = h.sent
        self.assertEqual(subject, "[P1] ALARM: monitoring-svc-api5xx-orders")
        for needle in ("API 5xx rate above 5%", "Runbook:  https://wiki.example/runbooks/5xx", "12x  db timeout", "POST /orders",
                       "xray/home?region=ap-south-1#/traces/1-bb", "dashboards:name=monitoring-overview", "alarmsV2:alarm/monitoring-svc-api5xx-orders"):
            self.assertIn(needle, text)
        self.assertLess(text.index("POST /orders"), text.index("GET /a"))  # failures listed before slow requests
        inc = h.store.latest_incident("monitoring-svc-api5xx-orders")
        self.assertEqual((inc["status"], inc["notify_latency_seconds"], inc["severity"]), ("OPEN", 7, "P1"))
        self.assertEqual(h.metric("NotifyLatencySeconds"), [7])

    def test_second_alarm_while_open_does_not_page_again(self):
        h = Harness()
        h.fire(alarm_event())
        result = h.fire(alarm_event(at=at(120)))
        self.assertEqual(result["action"], "duplicate_suppressed")
        self.assertEqual(len(h.sent), 1)
        self.assertEqual(h.store.latest_incident("monitoring-svc-api5xx-orders")["repeat_count"], 1)

    def test_recovery_closes_incident_and_records_duration(self):
        h = Harness()
        h.fire(alarm_event())
        h.now = T0 + timedelta(minutes=25)
        result = h.fire(alarm_event(state="OK", at=at(25 * 60 + 30)))
        self.assertEqual(result, {"action": "resolved", "duration_seconds": 25 * 60 + 30})
        inc = h.store.latest_incident("monitoring-svc-api5xx-orders")
        self.assertEqual((inc["status"], inc["duration_seconds"]), ("RESOLVED", 1530))
        self.assertIn("Recovered after 25 min 30 s", h.sent[-1][1])
        self.assertEqual(h.metric("IncidentDurationSeconds"), [1530])

    def test_ok_without_open_incident_is_ignored(self):
        h = Harness()
        self.assertEqual(h.fire(alarm_event(state="OK"))["action"], "ignored_no_open_incident")
        self.assertEqual(h.sent, [])

    def test_flapping_alarm_reopens_the_same_incident(self):
        h = Harness()
        h.fire(alarm_event())
        h.fire(alarm_event(state="OK", at=at(300)))
        h.now = T0 + timedelta(seconds=400)
        result = h.fire(alarm_event(at=at(400)))      # back 100 s after recovery, inside the 600 s window
        self.assertEqual(result["action"], "reopened")
        self.assertTrue(h.sent[-1][0].startswith("[P1] REOPENED ALARM"))
        self.assertIn("reopened 1 time", h.sent[-1][1])
        incidents = h.store.all_incidents()
        self.assertEqual(len(incidents), 1)
        self.assertEqual((incidents[0]["status"], incidents[0]["reopen_count"]), ("OPEN", 1))
        self.assertNotIn("resolved_at", incidents[0])
        h.fire(alarm_event(state="OK", at=at(1000)))
        self.assertEqual(h.store.latest_incident("monitoring-svc-api5xx-orders")["duration_seconds"], 1000)  # from first open

    def test_alarm_after_flap_window_is_a_new_incident(self):
        h = Harness()
        h.fire(alarm_event())
        h.fire(alarm_event(state="OK", at=at(300)))
        h.now = T0 + timedelta(seconds=300 + 601)
        self.assertEqual(h.fire(alarm_event(at=at(901)))["action"], "new")
        self.assertEqual(len(h.store.all_incidents()), 2)

    def test_failed_page_is_retried_not_suppressed(self):
        """If SNS fails, Lambda retries the event. The retry must page, not be treated as a duplicate."""
        h = Harness()
        h.fail_publish = 1
        with self.assertRaises(RuntimeError):
            h.fire(alarm_event())
        self.assertEqual(h.sent, [])
        result = h.fire(alarm_event())
        self.assertEqual(result["action"], "new")
        self.assertEqual(len(h.sent), 1)
        self.assertEqual(len(h.store.all_incidents()), 1)

    def test_insufficient_data_is_ignored(self):
        h = Harness()
        self.assertEqual(h.fire(alarm_event(state="INSUFFICIENT_DATA"))["action"], "ignored_insufficient_data")
        self.assertEqual(h.sent, [])

    def test_enrichment_failure_never_blocks_the_page(self):
        h = Harness()
        h.deps.find_logs = lambda *a: (_ for _ in ()).throw(RuntimeError("logs throttled"))
        self.assertEqual(h.fire(alarm_event())["action"], "new")
        self.assertEqual(len(h.sent), 1)
        self.assertTrue(any(l.get("type") == "enrichment_failed" for l in h.logs))

    def test_unsafe_service_names_are_not_put_into_log_queries(self):
        h = Harness()
        h.fire(alarm_event(dims={"Api": 'x" || $.a = "b'}))
        self.assertEqual(h.find_logs_calls, [])
        self.assertEqual(len(h.sent), 1)

    def test_webhook_failure_does_not_block_and_success_is_used(self):
        h = Harness()
        posted = []
        h.deps.webhook = lambda p: posted.append(p)
        h.fire(alarm_event())
        self.assertIn("API 5xx", posted[0]["text"])
        h2 = Harness()
        h2.deps.webhook = lambda p: 1 / 0
        self.assertEqual(h2.fire(alarm_event())["action"], "new")

    def test_subject_is_within_sns_limit(self):
        long_name = "monitoring-svc-" + "x" * 200
        h = Harness()
        h.fire(alarm_event(name=long_name))
        self.assertLessEqual(len(h.sent[0][0]), 100)

    def test_unknown_event_sources_are_ignored(self):
        self.assertEqual(Harness().fire({"source": "aws.s3"})["action"], "ignored")


class LogAlertTests(unittest.TestCase):
    def event(self, fp="abc123", count=3):
        return {"source": "monitoring.logs", "detail-type": "LogAlert", "time": "2026-11-19T10:00:00Z", "region": "ap-south-1",
                "detail": json.dumps({"service": "orders-fn", "reason": "timeout after 3.00s", "fingerprint": fp, "count": count,
                                      "sample": "Task timed out after 3.00 seconds", "log_group": "/aws/lambda/orders-fn",
                                      "request_id": "req-1", "trace_id": "1-trace"})}

    def test_critical_log_pages_immediately_without_an_alarm(self):
        h = Harness()
        self.assertEqual(h.fire(self.event())["action"], "log_alert_sent")
        [(subject, text)] = h.sent
        self.assertTrue(subject.startswith("[P2] LOG: log-alert:orders-fn"))
        self.assertIn("timeout after 3.00s", text)
        self.assertIn("seen 3x", text)
        self.assertIn("traces/1-trace", text)

    def test_same_problem_is_suppressed_until_the_window_passes(self):
        h = Harness(log_alert_dedup_seconds=900)
        h.fire(self.event())
        self.assertEqual(h.fire(self.event())["action"], "duplicate_suppressed")
        self.assertEqual(h.fire(self.event(fp="different"))["action"], "log_alert_sent")
        h.now += timedelta(seconds=901)
        self.assertEqual(h.fire(self.event())["action"], "log_alert_sent")
        self.assertEqual(len(h.sent), 3)

    def test_failed_page_releases_the_dedup_key_so_the_retry_pages(self):
        h = Harness()
        h.fail_publish = 1
        with self.assertRaises(RuntimeError):
            h.fire(self.event())
        self.assertEqual(h.fire(self.event())["action"], "log_alert_sent")
        self.assertEqual(len(h.sent), 1)


if __name__ == "__main__":
    unittest.main()
