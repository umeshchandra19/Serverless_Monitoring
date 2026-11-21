"""Lambda: receives CloudWatch alarm state changes and critical-log events from EventBridge, then pages with context."""
import json
import os
import urllib.request
from datetime import datetime, timezone

from monitoring import aws
from monitoring.router import RouterDeps, handle_event
from monitoring.store import DynamoStore

_deps = None


def _webhook(url):
    def post(payload):
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
    return post


def _find_logs_factory():
    client = aws.logs_client()

    def find_logs(log_group, pattern, start_ms, limit):
        try:
            resp = client.filter_log_events(logGroupName=log_group, filterPattern=pattern, startTime=start_ms, limit=limit)
        except client.exceptions.ResourceNotFoundException:
            return []
        return [e["message"] for e in resp.get("events", [])]
    return find_logs


def build_deps():
    env = os.environ
    return RouterDeps(
        store=DynamoStore(aws.incident_table(env["INCIDENT_TABLE"])),
        publish=aws.sns_publisher(env["ALERT_TOPIC_ARN"]),
        now=lambda: datetime.now(timezone.utc),
        find_logs=_find_logs_factory(),
        webhook=_webhook(env["SLACK_WEBHOOK_URL"]) if env.get("SLACK_WEBHOOK_URL") else None,
        config={"region": env.get("AWS_REGION", ""), "dashboard_name": env.get("DASHBOARD_NAME", ""),
                "log_ingest_group": env.get("LOG_INGEST_LOG_GROUP"), "api_ingest_group": env.get("API_INGEST_LOG_GROUP"),
                "flap_window_seconds": int(env.get("FLAP_WINDOW_SECONDS", "600")),
                "log_alert_dedup_seconds": int(env.get("LOG_ALERT_DEDUP_SECONDS", "900")),
                "log_alert_severity": env.get("LOG_ALERT_SEVERITY", "P2")})


def handler(event, context):
    global _deps
    if _deps is None:
        _deps = build_deps()
    return handle_event(event, _deps)
