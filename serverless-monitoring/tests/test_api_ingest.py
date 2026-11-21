import json
import unittest

from tests.helpers import Capture, T0_MS, logs_event
from handlers import api_ingest


def access(status=200, method="GET", path="/orders", latency=50, integration=40, trace="1-abc-def", error="-", rid="r"):
    return json.dumps({"requestId": rid, "httpMethod": method, "resourcePath": path, "status": str(status),
                       "responseLatency": str(latency), "integrationLatency": str(integration) if integration is not None else "-",
                       "errorMessage": error, "traceId": trace})


class ApiIngestTests(unittest.TestCase):
    def run_ingest(self, lines, **config):
        cap = Capture(**config)
        result = api_ingest.process(logs_event("/aws/apigateway/orders-api", lines, [T0_MS] * len(lines)), cap.deps)
        return cap, result

    def test_status_classes_and_totals(self):
        lines = [access(200)] * 6 + [access(404)] * 2 + [access(500)] + [access(503)] + [access(301)]
        cap, result = self.run_ingest(lines)
        for name, want in (("RequestCount", 11), ("Http2xx", 6), ("Http3xx", 1), ("Http4xx", 2), ("Http5xx", 2)):
            self.assertEqual(cap.metric(name, Api="orders-api")[0][1], want, name)
        self.assertEqual(result["requests"], 11)

    def test_api_level_and_route_level_metrics(self):
        lines = [access(200, path="/orders")] * 3 + [access(500, "POST", "/orders")] + [access(200, path="/items/{id}")] * 2
        cap, _ = self.run_ingest(lines)
        self.assertEqual(cap.metric("RequestCount", Api="orders-api")[0][1], 6)               # whole API
        self.assertEqual(cap.metric("RequestCount", Route="GET /orders")[0][1], 3)            # per route
        self.assertEqual(cap.metric("Http5xx", Route="POST /orders")[0][1], 1)
        self.assertEqual(cap.metric("RequestCount", Route="GET /items/{id}")[0][1], 2)

    def test_latency_distribution_and_slow_requests(self):
        lines = [access(200, latency=v) for v in (100, 200, 3000)]
        cap, _ = self.run_ingest(lines, slow_request_ms=1000)
        self.assertEqual(sorted(cap.metric("Latency", Api="orders-api")[0][1]), [100.0, 200.0, 3000.0])
        self.assertEqual(cap.metric("SlowRequests", Api="orders-api")[0][1], 1)

    def test_missing_integration_latency_is_ignored_not_zero(self):
        cap, _ = self.run_ingest([access(200, integration=None), access(200, integration=30)])
        self.assertEqual(cap.metric("IntegrationLatency", Api="orders-api")[0][1], [30.0])

    def test_exemplars_prefer_failures_and_carry_the_trace_id(self):
        lines = [access(200, latency=4000, rid="slow", trace="1-slow"), access(500, rid="bad", trace="1-bad", error="Internal"), access(200, rid="fast")]
        cap, result = self.run_ingest(lines)
        ex = [l for l in cap.logs if l.get("type") == "api_exemplar"]
        self.assertEqual([e["request_id"] for e in ex], ["bad", "slow"])
        self.assertEqual((ex[0]["reason"], ex[0]["trace_id"], ex[0]["api"]), ("5xx", "1-bad", "orders-api"))
        self.assertEqual(ex[1]["reason"], "slow")
        self.assertEqual(result["exemplars"], 2)

    def test_exemplar_cap(self):
        cap, _ = self.run_ingest([access(500, rid=str(i)) for i in range(30)], max_exemplars=4)
        self.assertEqual(sum(1 for l in cap.logs if l.get("type") == "api_exemplar"), 4)

    def test_route_cardinality_guard(self):
        lines = [access(200, path=f"/p{i}") for i in range(60)]
        cap, _ = self.run_ingest(lines, max_routes=10)
        routes = {r["Route"] for r in cap.emitted if "Route" in r}
        self.assertEqual(len(routes), 11)
        self.assertIn("OTHER", routes)

    def test_unparseable_lines_are_counted(self):
        cap, result = self.run_ingest(["not json", access(200), '{"no":"status"}'])
        self.assertEqual(result["unparsed"], 2)
        self.assertEqual(cap.metric("ParseFailures", Api="orders-api")[0][1], 2)

    def test_own_output_and_other_log_groups(self):
        cap, result = self.run_ingest(['{"type":"api_exemplar","status":500}', '{"_aws":{},"status":1}', access(200)])
        self.assertEqual(result["requests"], 1)
        cap = Capture(ignore_prefixes=["/aws/lambda/monitoring-"])
        self.assertEqual(api_ingest.process(logs_event("/aws/lambda/monitoring-x", [access(200)]), cap.deps)["status"], "ignored_own_log_group")

    def test_metrics_land_in_the_minute_the_request_happened(self):
        cap = Capture()
        lines = [access(200), access(500)]
        api_ingest.process(logs_event("/aws/apigateway/a", lines, [T0_MS, T0_MS + 120_000]), cap.deps)
        self.assertEqual([t for t, _ in cap.metric("RequestCount", without=("Route",), Api="a")], [T0_MS, T0_MS + 120_000])


if __name__ == "__main__":
    unittest.main()
