#!/usr/bin/env python3
"""Run the whole pipeline locally on SYNTHETIC traffic: no AWS account needed.

    python tools/simulate.py

It generates 40 minutes of fake API and application logs with an outage in the middle, feeds them through the real
log_ingest / api_ingest handlers, evaluates the 5xx alarm rule locally, and passes the alarm to the real alert router.
What is real: the handler and router code. What is simulated: the traffic, the CloudWatch alarm evaluation (a local
approximation of "3 of 5 one-minute datapoints above the threshold") and the delivery delays (stated below).
"""
import json
import os
import random
import re
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from handlers import api_ingest, log_ingest  # noqa: E402
from monitoring.cwlogs import encode_subscription_event  # noqa: E402
from monitoring.ingest import IngestDeps  # noqa: E402
from monitoring.router import RouterDeps, handle_event  # noqa: E402
from monitoring.store import InMemoryStore  # noqa: E402

START = datetime(2026, 11, 19, 10, 0, 0, tzinfo=timezone.utc)
MINUTES, OUTAGE = 40, range(14, 28)
METRIC_DELAY_S = 90          # assumed: log line -> subscription -> Lambda -> metric visible to the alarm
ALARM_NAME = "monitoring-svc-api5xx-orders-api"
DESCRIPTION = "severity=P1; runbook=https://wiki.example.com/runbooks/api-5xx; 5xx rate of orders-api above 5% (ignored below 20 requests/min)"


def make_traffic(rnd):
    """Returns {minute: (api_lines, app_lines)}."""
    traffic = {}
    for m in range(MINUTES):
        out = m in OUTAGE
        api, app = [], []
        for i in range(rnd.randint(36, 48)):
            failed = rnd.random() < (0.38 if out else 0.01)
            latency = rnd.randint(1500, 4200) if out else rnd.randint(60, 240)
            rid, trace = f"req-{m}-{i}", f"1-6740{m:02d}{i:02d}-{rnd.getrandbits(64):016x}"
            status = rnd.choice([500, 502, 503]) if failed else rnd.choice([200] * 9 + [404])
            api.append(json.dumps({"requestId": rid, "httpMethod": "GET", "resourcePath": "/orders", "status": str(status),
                                   "responseLatency": str(latency), "integrationLatency": str(latency - 4), "traceId": trace,
                                   "errorMessage": "Internal server error" if failed else "-"}))
            if failed:
                app.append(json.dumps({"level": "error", "message": f"database timeout while loading order {rnd.randint(1000, 9999)}",
                                       "error_type": "DatabaseTimeout", "request_id": rid, "trace_id": trace, "latency_ms": latency}))
            else:
                app.append(json.dumps({"level": "info", "message": "request served", "status": status, "latency_ms": latency, "request_id": rid}))
        if m == 16:
            app.append("2026-11-19T10:16:07Z req-x Task timed out after 10.00 seconds")
        traffic[m] = (api, app)
    return traffic


def evaluate_5xx_rule(rates, min_requests=20, threshold=5.0, window=5, needed=3):
    """Local approximation of the generated alarm: ALARM when >= `needed` of the last `window` datapoints breach."""
    states, history = [], []
    for requests, errors in rates:
        history.append(100 * errors / requests if requests >= min_requests else 0.0)
        recent = history[-window:]
        states.append("ALARM" if sum(1 for r in recent if r > threshold) >= needed else "OK")
    return states


def event_time(minute):
    return START + timedelta(minutes=minute + 1, seconds=METRIC_DELAY_S)


def alarm_event(state, at):
    return {"version": "0", "detail-type": "CloudWatch Alarm State Change", "source": "aws.cloudwatch", "account": "123456789012",
            "time": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "region": "ap-south-1",
            "detail": {"alarmName": ALARM_NAME, "state": {"value": state, "reason": "Threshold Crossed: 3 out of the last 5 datapoints were greater than the threshold (5.0).",
                                                          "timestamp": at.strftime("%Y-%m-%dT%H:%M:%S.000+0000")},
                       "configuration": {"description": DESCRIPTION, "metrics": [{"id": "errors", "metricStat": {"metric": {
                           "namespace": "Monitoring/Api", "name": "Http5xx", "dimensions": {"Api": "orders-api", "Service": "orders-fn"}}, "period": 60, "stat": "Sum"}}]}}}


def main():
    rnd = random.Random(42)
    traffic = make_traffic(rnd)
    emitted, logs, events = [], [], []
    deps = IngestDeps(emit=lambda r: emitted.extend(r), log=logs.append, put_events=lambda e: events.extend(e) or 0, config={"alert_patterns": []})

    print("SIMULATION: synthetic traffic, real handler code, no AWS\n" + "=" * 60)
    print(f"Traffic: {MINUTES} minutes, outage in minutes {OUTAGE.start}-{OUTAGE.stop - 1} (database timeouts)")
    for m, (api, app) in traffic.items():
        ts = int((START + timedelta(minutes=m, seconds=30)).timestamp() * 1000)
        api_ingest.process(encode_subscription_event("/aws/apigateway/orders-api", "s", api, [ts] * len(api)), deps)
        log_ingest.process(encode_subscription_event("/aws/lambda/orders-fn", "s", app, [ts] * len(app)), deps)
    api_level = {}
    for r in emitted:
        if r.get("Api") == "orders-api" and "Route" not in r and "RequestCount" in r:
            api_level[(r["_aws"]["Timestamp"] - int(START.timestamp() * 1000)) // 60000] = r
    print(f"Handlers produced {len(emitted)} metric records, {len(logs)} structured log lines, {len(events)} critical-log event(s)\n")

    print("minute  requests  5xx   5xx%   p95 latency")
    rates = []
    for m in range(MINUTES):
        r = api_level[m]
        lat = sorted(r["Latency"])
        rates.append((r["RequestCount"], r["Http5xx"]))
        if m % 3 == 0 or m in (13, 14, 27, 28):
            print(f"{m:>5}  {r['RequestCount']:>8}  {r['Http5xx']:>3}  {100 * r['Http5xx'] / r['RequestCount']:>5.1f}  {lat[int(len(lat) * .95) - 1]:>8.0f} ms")

    states = evaluate_5xx_rule(rates)
    changes = [(m, s) for m, s in enumerate(states) if m == 0 and s != "OK" or m > 0 and s != states[m - 1]]
    print(f"\nAlarm rule (local evaluation): {changes}")

    store, sent = InMemoryStore(), []
    clock = {"now": START}

    def find_logs(group, pattern, start_ms, limit):
        match = re.search(r'\$\.type = "(\w+)" && \$\.(service|api) = "([^"]+)"', pattern)
        typ, key, val = match.groups()
        return [json.dumps(l) for l in logs if l.get("type") == typ and l.get(key) == val][:limit]

    rdeps = RouterDeps(store=store, publish=lambda s, m: sent.append((clock["now"], s, m)), now=lambda: clock["now"], find_logs=find_logs,
                       emit=lambda r: None, log=lambda o: None,
                       config={"region": "ap-south-1", "dashboard_name": "monitoring-overview", "log_ingest_group": "g1", "api_ingest_group": "g2"})
    for entry in events:       # the instant, event-driven path: critical log lines page without waiting for an alarm
        ev = {"source": entry["Source"], "detail-type": entry["DetailType"], "detail": entry["Detail"],
              "time": (START + timedelta(minutes=16, seconds=40)).strftime("%Y-%m-%dT%H:%M:%SZ"), "region": "ap-south-1"}
        clock["now"] = START + timedelta(minutes=16, seconds=41)
        handle_event(ev, rdeps)
    for m, state in changes:
        at = event_time(m)
        clock["now"] = at + timedelta(seconds=6)       # assumed delivery delay: EventBridge -> Lambda -> SNS
        handle_event(alarm_event(state, at), rdeps)

    print("\n" + "=" * 60 + "\nNOTIFICATIONS SENT\n" + "=" * 60)
    for when, subject, body in sent:
        print(f"\n--- {when:%H:%M:%S} UTC | subject: {subject}\n{body}")
    inc = store.latest_incident(ALARM_NAME)
    print("\n" + "=" * 60 + "\nINCIDENT LEDGER\n" + "=" * 60)
    print(json.dumps({k: inc[k] for k in ("status", "severity", "opened_at", "resolved_at", "duration_seconds", "notify_latency_seconds")}, indent=2))
    print("\nNote: all numbers above come from synthetic data and stated assumptions "
          f"(metric delay {METRIC_DELAY_S}s). They illustrate how the pipeline behaves; they are not a measurement of your systems.")


if __name__ == "__main__":
    main()
