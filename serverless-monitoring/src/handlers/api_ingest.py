"""Lambda: API Gateway access logs (CloudWatch Logs subscription) -> API metrics and trace exemplars.

Access-log format (set in template.yaml) is one JSON object per request. For each minute and route it emits (EMF):
  Monitoring/Api  dimensions [Api] and [Api, Route]:
    RequestCount, Http2xx, Http3xx, Http4xx, Http5xx, SlowRequests,
    Latency (distribution, ms), IntegrationLatency (distribution, ms)
It also logs "exemplars" (failed or slow requests with their X-Ray trace id) so an alert can link straight to a trace.
"""
import json
from collections import defaultdict

from monitoring import emf
from monitoring.cwlogs import decode_subscription_event, service_from_log_group
from monitoring.ingest import ignored_log_group, lambda_deps

MINUTE_MS = 60_000


def _num(value):
    try:
        n = float(value)
        return n if n == n and abs(n) != float("inf") else None
    except (TypeError, ValueError):
        return None  # API Gateway writes "-" when a value does not exist


def parse_access_log(message):
    """Returns a dict with normalised fields, or None if the line is not an access-log JSON object."""
    try:
        data = json.loads(message)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or "status" not in data or "_aws" in data or data.get("type") in ("api_exemplar", "ingest_summary", "error_signature"):
        return None
    status = _num(data.get("status"))
    if status is None:
        return None
    method = str(data.get("httpMethod") or data.get("method") or "").upper()
    path = str(data.get("resourcePath") or data.get("routeKey") or data.get("path") or "")
    route = f"{method} {path}".strip() or "unknown"
    return {"status": int(status), "route": route, "latency": _num(data.get("responseLatency")),
            "integration": _num(data.get("integrationLatency")), "request_id": data.get("requestId"),
            "trace_id": data.get("traceId") if data.get("traceId") not in ("-", "") else None,
            "error": data.get("errorMessage") if data.get("errorMessage") not in ("-", "") else None}


class _Stats:
    def __init__(self):
        self.count = self.s2 = self.s3 = self.s4 = self.s5 = self.slow = 0
        self.latency, self.integration = [], []

    def add(self, req, slow_ms):
        self.count += 1
        klass = req["status"] // 100
        if klass == 2: self.s2 += 1
        elif klass == 3: self.s3 += 1
        elif klass == 4: self.s4 += 1
        elif klass >= 5: self.s5 += 1
        if req["latency"] is not None:
            self.latency.append(req["latency"])
            if req["latency"] > slow_ms:
                self.slow += 1
        if req["integration"] is not None:
            self.integration.append(req["integration"])

    def metrics(self):
        return [("RequestCount", self.count, "Count"), ("Http2xx", self.s2, "Count"), ("Http3xx", self.s3, "Count"),
                ("Http4xx", self.s4, "Count"), ("Http5xx", self.s5, "Count"), ("SlowRequests", self.slow, "Count"),
                ("Latency", self.latency, "Milliseconds"), ("IntegrationLatency", self.integration, "Milliseconds")]


def process(event, deps):
    payload = decode_subscription_event(event)
    if payload is None:
        return {"status": "ignored"}
    cfg = deps.config
    log_group = payload["logGroup"]
    if ignored_log_group(log_group, cfg.get("ignore_prefixes", [])):
        return {"status": "ignored_own_log_group"}
    api = service_from_log_group(log_group, cfg.get("service_map"))
    slow_ms, max_routes = cfg.get("slow_request_ms", 1000), cfg.get("max_routes", 50)

    per_api, per_route, requests, failures = defaultdict(_Stats), defaultdict(_Stats), [], 0
    known_routes = set()
    for e in payload.get("logEvents", []):
        req = parse_access_log(e.get("message", ""))
        if req is None:
            failures += 1
            continue
        minute = int(e.get("timestamp") or deps.now_ms()) // MINUTE_MS * MINUTE_MS
        if req["route"] not in known_routes and len(known_routes) >= max_routes:
            req["route"] = "OTHER"  # cardinality guard: unbounded route names would explode metric cost
        known_routes.add(req["route"])
        per_api[minute].add(req, slow_ms)
        per_route[(minute, req["route"])].add(req, slow_ms)
        requests.append(req)

    ns = cfg.get("api_namespace", "Monitoring/Api")
    for minute, stats in sorted(per_api.items()):
        extra = [("ParseFailures", failures, "Count")] if failures and minute == min(per_api) else []
        deps.emit(emf.build_records(ns, [["Api"]], {"Api": api}, stats.metrics() + extra, timestamp_ms=minute))
    for (minute, route), stats in sorted(per_route.items()):
        deps.emit(emf.build_records(ns, [["Api", "Route"]], {"Api": api, "Route": route}, stats.metrics(), timestamp_ms=minute))

    exemplars = _exemplars(requests, slow_ms, cfg.get("max_exemplars", 10))
    for ex in exemplars:
        deps.log({"type": "api_exemplar", "api": api, **ex})
    deps.log({"type": "ingest_summary", "api": api, "requests": len(requests), "unparsed": failures})
    return {"status": "ok", "api": api, "requests": len(requests), "unparsed": failures, "exemplars": len(exemplars)}


def _exemplars(requests, slow_ms, limit):
    failed = [r for r in requests if r["status"] >= 500]
    slow = sorted((r for r in requests if r["status"] < 500 and (r["latency"] or 0) > slow_ms), key=lambda r: -r["latency"])
    out = []
    for reason, group in (("5xx", failed), ("slow", slow)):
        for r in group:
            if len(out) >= limit:
                return out
            out.append({"reason": reason, "route": r["route"], "status": r["status"], "latency_ms": r["latency"],
                        "integration_latency_ms": r["integration"], "request_id": r["request_id"],
                        "trace_id": r["trace_id"], "error": r["error"]})
    return out


def handler(event, context):
    return process(event, lambda_deps())
