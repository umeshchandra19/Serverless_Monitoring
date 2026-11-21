"""Lambda: application logs (CloudWatch Logs subscription) -> metrics, error signatures, critical-event alerts.

For every batch of log lines it
  1. parses each line (JSON or plain text) and groups lines by minute,
  2. emits per-minute metrics via EMF  (Monitoring/Logs, dimension Service):
        LogEvents, ErrorCount, WarnCount, CriticalCount, ParseFailures,
        RequestLatencyMs (distribution), LambdaDurationMs (distribution), LambdaMaxMemoryMb
  3. logs the most frequent error signatures (used later to enrich alerts),
  4. publishes an EventBridge "LogAlert" event for critical problems (timeouts, out-of-memory, CRITICAL logs).
"""
import json
import re

from monitoring import emf
from monitoring.aggregate import aggregate_by_minute, error_signatures
from monitoring.cwlogs import decode_subscription_event, service_from_log_group
from monitoring.ingest import ignored_log_group, lambda_deps
from monitoring.logparse import fingerprint, parse_line

MAX_SIGNATURE_LOGS = 5


def process(event, deps):
    payload = decode_subscription_event(event)
    if payload is None:
        return {"status": "ignored"}
    cfg = deps.config
    log_group, log_stream = payload["logGroup"], payload.get("logStream", "")
    if ignored_log_group(log_group, cfg.get("ignore_prefixes", [])):
        return {"status": "ignored_own_log_group"}
    service = service_from_log_group(log_group, cfg.get("service_map"))

    parsed = [(e.get("timestamp") or deps.now_ms(), parse_line(e.get("message", ""))) for e in payload.get("logEvents", [])]
    buckets = aggregate_by_minute(parsed)
    records = [r for _, r in parsed if not r.own_output]

    for minute_ms, b in sorted(buckets.items()):
        metrics = [("LogEvents", b.events, "Count"), ("ErrorCount", b.errors, "Count"), ("WarnCount", b.warns, "Count"),
                   ("CriticalCount", b.criticals, "Count"), ("ParseFailures", b.parse_failures, "Count"),
                   ("RequestLatencyMs", b.latencies, "Milliseconds"), ("LambdaDurationMs", b.lambda_durations, "Milliseconds"),
                   ("LambdaInitMs", b.lambda_init, "Milliseconds")]
        if b.max_memory_mb is not None:
            metrics.append(("LambdaMaxMemoryMb", b.max_memory_mb, "Megabytes"))
        deps.emit(emf.build_records(cfg.get("log_namespace", "Monitoring/Logs"), [["Service"]], {"Service": service},
                                    metrics, timestamp_ms=minute_ms))

    signatures = error_signatures(records, limit=MAX_SIGNATURE_LOGS)
    for sig in signatures:
        deps.log({"type": "error_signature", "service": service, "fingerprint": sig.fingerprint, "count": sig.count,
                  "severity": sig.severity, "sample": sig.sample, "request_id": sig.request_id, "trace_id": sig.trace_id,
                  "log_group": log_group})

    alerts = _critical_alerts(records, service, log_group, log_stream, cfg, deps)
    failed = 0
    if alerts and deps.put_events:
        try:
            failed = deps.put_events(alerts)
        except Exception as exc:  # losing an alert must not lose the metrics already emitted
            deps.log({"type": "ingest_summary", "service": service, "alert_publish_error": str(exc)[:200]})
            failed = len(alerts)
    summary = {"type": "ingest_summary", "service": service, "log_events": len(parsed),
               "alerts_published": len(alerts) - failed, "alerts_failed": failed}
    deps.log(summary)
    return {"status": "ok", **{k: v for k, v in summary.items() if k != "type"}}


def _critical_alerts(records, service, log_group, log_stream, cfg, deps):
    patterns = [re.compile(p, re.I) for p in cfg.get("alert_patterns", [])]
    seen, entries = {}, []
    for rec in records:
        reason = rec.critical_reason
        if not reason:
            reason = next((f"matched /{p.pattern}/" for p in patterns if p.search(rec.message)), None)
        if not reason:
            continue
        fp = fingerprint(rec.message)
        if fp in seen:
            seen[fp]["count"] += 1
            continue
        if len(seen) >= cfg.get("max_alerts_per_invocation", 10):
            continue  # alert storm guard: the metrics still show the full volume
        seen[fp] = {"service": service, "reason": reason, "fingerprint": fp, "sample": rec.message[:300], "count": 1,
                    "log_group": log_group, "log_stream": log_stream, "request_id": rec.request_id, "trace_id": rec.trace_id}
    for detail in seen.values():
        entries.append({"Source": "monitoring.logs", "DetailType": "LogAlert", "Detail": json.dumps(detail),
                        "EventBusName": cfg.get("bus_name", "default")})
    return entries


def handler(event, context):
    return process(event, lambda_deps())
