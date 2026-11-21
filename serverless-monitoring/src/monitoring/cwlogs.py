"""Helpers for CloudWatch Logs subscription events (what a Lambda subscribed to a log group receives)."""
import base64
import gzip
import json
import time


def decode_subscription_event(event):
    """Returns the decoded payload dict, or None for control messages / malformed events."""
    try:
        raw = base64.b64decode(event["awslogs"]["data"])
        payload = json.loads(gzip.decompress(raw))
    except (KeyError, TypeError, ValueError, OSError):
        return None
    return payload if payload.get("messageType") == "DATA_MESSAGE" else None


def encode_subscription_event(log_group, log_stream, messages, timestamps_ms=None):
    """Build the event CloudWatch would send. Used by tests and the local simulator."""
    now = int(time.time() * 1000)
    events = [{"id": str(i), "timestamp": (timestamps_ms[i] if timestamps_ms else now), "message": m}
              for i, m in enumerate(messages)]
    payload = {"messageType": "DATA_MESSAGE", "owner": "123456789012", "logGroup": log_group, "logStream": log_stream,
               "subscriptionFilters": ["monitoring"], "logEvents": events}
    return {"awslogs": {"data": base64.b64encode(gzip.compress(json.dumps(payload).encode())).decode()}}


def service_from_log_group(log_group, overrides=None):
    """Derives the 'service' name used as a metric dimension. A SERVICE_MAP override always wins."""
    if overrides and log_group in overrides:
        return overrides[log_group]
    name = log_group.strip("/")
    if name.startswith("API-Gateway-Execution-Logs_"):
        return name.split("/")[0].replace("API-Gateway-Execution-Logs_", "api-")
    for prefix in ("aws/lambda/", "aws/apigateway/", "aws/ecs/", "ecs/", "aws/eks/", "aws/codebuild/"):
        if name.startswith(prefix):
            return name[len(prefix):].split("/")[0] or name
    return name.split("/")[-1] or name
