import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

from tests import helpers  # noqa: F401
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import incident_report as ir  # noqa: E402
import subscribe_log_group as sub  # noqa: E402
from demo_app import app as demo  # noqa: E402
from monitoring.logparse import parse_line  # noqa: E402


class FakeLogs:
    def __init__(self, existing=()):
        self.filters, self.calls = [{"filterName": n} for n in existing], []

    def describe_subscription_filters(self, logGroupName):
        return {"subscriptionFilters": self.filters}

    def put_subscription_filter(self, **kw):
        self.calls.append(("put", kw))

    def delete_subscription_filter(self, **kw):
        self.calls.append(("delete", kw))


class FakeLambda:
    def get_function(self, FunctionName):
        return {"Configuration": {"FunctionArn": f"arn:aws:lambda:ap-south-1:1:function:{FunctionName}"}}


class SubscribeTests(unittest.TestCase):
    def run_cli(self, args, logs):
        out = io.StringIO()
        with redirect_stdout(out):
            sub.main(args, logs=logs, lam=FakeLambda())
        return out.getvalue()

    def test_creates_subscription_to_the_right_function(self):
        logs = FakeLogs()
        out = self.run_cli(["--log-group", "/ecs/orders", "--kind", "api"], logs)
        self.assertIn("create", out)
        [(_, kw)] = logs.calls
        self.assertTrue(kw["destinationArn"].endswith("function:monitoring-api-ingest"))
        self.assertEqual((kw["filterName"], kw["filterPattern"]), ("monitoring-api", ""))

    def test_update_is_idempotent_and_dry_run_changes_nothing(self):
        logs = FakeLogs(existing=["monitoring-app"])
        self.assertIn("update", self.run_cli(["--log-group", "/x", "--kind", "app"], logs))
        logs2 = FakeLogs()
        self.run_cli(["--log-group", "/x", "--kind", "app", "--dry-run"], logs2)
        self.assertEqual(logs2.calls, [])

    def test_refuses_when_two_other_filters_exist(self):
        with self.assertRaises(SystemExit):
            self.run_cli(["--log-group", "/x", "--kind", "app"], FakeLogs(existing=["a", "b"]))

    def test_remove(self):
        logs = FakeLogs(existing=["monitoring-app"])
        self.assertIn("removed", self.run_cli(["--log-group", "/x", "--kind", "app", "--remove"], logs))
        self.assertEqual(logs.calls[0][0], "delete")
        self.assertIn("absent", self.run_cli(["--log-group", "/x", "--kind", "app", "--remove"], FakeLogs()))


def incident(opened, minutes, status="RESOLVED", sev="P1", notify=20, reopen=0):
    return {"opened_at": opened, "status": status, "severity": sev, "duration_seconds": minutes * 60 if status == "RESOLVED" else None,
            "notify_latency_seconds": notify, "reopen_count": reopen}


class IncidentReportTests(unittest.TestCase):
    def test_summary_statistics(self):
        rows = [incident(f"2026-11-0{d}T10:00:00+00:00", m) for d, m in ((1, 10), (2, 20), (3, 30), (4, 40))]
        rows.append(incident("2026-11-05T10:00:00+00:00", 0, status="OPEN", reopen=1))
        s = ir.summarize(rows)
        self.assertEqual((s["incidents"], s["resolved"], s["open"], s["flapping"]), (5, 4, 1, 1))
        self.assertEqual((s["mttr_mean_s"], s["mttr_median_s"]), (1500, 1500))
        self.assertEqual(s["notify_max_s"], 20)

    def test_date_filter(self):
        rows = [incident("2026-10-01T00:00:00Z", 5), incident("2026-11-10T00:00:00Z", 5)]
        self.assertEqual(ir.summarize(rows, since=ir.parse_ts("2026-11-01"))["incidents"], 1)
        self.assertEqual(ir.summarize(rows, until=ir.parse_ts("2026-11-01"))["incidents"], 1)

    def test_empty_input_does_not_crash(self):
        s = ir.summarize([])
        self.assertIsNone(s["mttr_mean_s"])
        self.assertIn("n/a", ir.render(s))

    def test_baseline_comparison_and_small_sample_warning(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
            f.write("opened_at,resolved_at\n2026-06-01T10:00:00Z,2026-06-01T11:00:00Z\n2026-06-02T10:00:00Z,2026-06-02T11:30:00Z\n")
        base = ir.summarize(ir.load_baseline(f.name))
        os.unlink(f.name)
        self.assertEqual(base["mttr_mean_s"], 4500)
        now = ir.summarize([incident("2026-11-01T10:00:00Z", 45), incident("2026-11-02T10:00:00Z", 45)])
        text = ir.render(now, base)
        self.assertIn("-40% vs baseline (faster)", text)
        self.assertIn("fewer than 10", text)

    def test_cli_from_json(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump([incident("2026-11-01T10:00:00Z", 15)], f)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(ir.main(["--from-json", f.name]), 0)
        os.unlink(f.name)
        self.assertIn("15m 0s", out.getvalue())

    def test_dynamodb_decimals_are_handled(self):
        from decimal import Decimal
        row = {"opened_at": "2026-11-01T10:00:00Z", "status": "RESOLVED", "duration_seconds": Decimal("600"), "notify_latency_seconds": Decimal("12"), "reopen_count": Decimal("0")}
        self.assertEqual(ir.summarize([row])["mttr_mean_s"], 600)


class Ctx:
    aws_request_id = "req-123"


class DemoAppTests(unittest.TestCase):
    def call(self, method="GET", resource="/orders", qs=None, path_params=None):
        out = io.StringIO()
        with redirect_stdout(out):
            resp = demo.handler({"httpMethod": method, "resource": resource, "queryStringParameters": qs, "pathParameters": path_params}, Ctx())
        return resp, [l for l in out.getvalue().splitlines() if l]

    def setUp(self):
        os.environ["FAILURE_RATE"] = "0"

    def test_success_logs_are_parseable_and_carry_latency(self):
        resp, logs = self.call()
        self.assertEqual(resp["statusCode"], 200)
        rec = parse_line(logs[0])
        self.assertEqual((rec.severity, rec.request_id), ("INFO", "req-123"))
        self.assertIsNotNone(rec.latency_ms)

    def test_failure_injection_logs_a_critical_looking_error(self):
        resp, logs = self.call(qs={"fail": "1"})
        self.assertEqual(resp["statusCode"], 500)
        rec = parse_line(logs[0])
        self.assertEqual(rec.severity, "ERROR")
        self.assertIn("DatabaseTimeout", rec.message)

    def test_random_failure_rate(self):
        os.environ["FAILURE_RATE"] = "1"
        self.assertEqual(self.call()[0]["statusCode"], 500)

    def test_routes(self):
        self.assertEqual(self.call(resource="/health")[0]["statusCode"], 200)
        self.assertEqual(self.call(resource="/orders/{id}", path_params={"id": "2"})[0]["statusCode"], 200)
        self.assertEqual(self.call(resource="/orders/{id}", path_params={"id": "999"})[0]["statusCode"], 404)
        self.assertEqual(self.call(method="POST")[0]["statusCode"], 201)

    def test_slow_is_capped(self):
        import time
        start = time.time()
        self.call(qs={"slow": "50"})
        self.assertGreaterEqual(time.time() - start, 0.05)


if __name__ == "__main__":
    unittest.main()
