"""Shared plumbing for the two log-driven Lambdas."""
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Callable

from . import aws, emf


@dataclass
class IngestDeps:
    emit: Callable = field(default=emf.emit)                       # prints EMF records
    log: Callable = field(default=lambda obj: print(json.dumps(obj, separators=(",", ":"), default=str)))
    put_events: Callable = None                                    # EventBridge sender (returns failed count)
    now_ms: Callable = field(default=lambda: int(time.time() * 1000))
    config: dict = field(default_factory=dict)


def default_config():
    return {
        "service_map": aws.env_json("SERVICE_MAP", {}),
        "ignore_prefixes": [p for p in os.environ.get("IGNORE_LOG_GROUP_PREFIXES", "").split(",") if p],
        "alert_patterns": aws.env_json("ALERT_PATTERNS", []),
        "max_alerts_per_invocation": int(os.environ.get("MAX_ALERTS_PER_INVOCATION", "10")),
        "slow_request_ms": float(os.environ.get("SLOW_REQUEST_MS", "1000")),
        "max_exemplars": int(os.environ.get("MAX_EXEMPLARS", "10")),
        "max_routes": int(os.environ.get("MAX_ROUTES_PER_BATCH", "50")),
        "bus_name": os.environ.get("EVENT_BUS_NAME", "default"),
        "log_namespace": os.environ.get("LOG_METRIC_NAMESPACE", "Monitoring/Logs"),
        "api_namespace": os.environ.get("API_METRIC_NAMESPACE", "Monitoring/Api"),
    }


_cached = None


def lambda_deps():
    global _cached
    if _cached is None:
        _cached = IngestDeps(put_events=aws.put_events_client(), config=default_config())
    return _cached


def ignored_log_group(log_group, prefixes):
    """The pipeline must never process its own logs (that would be an infinite loop)."""
    return any(log_group.startswith(p) for p in prefixes)
