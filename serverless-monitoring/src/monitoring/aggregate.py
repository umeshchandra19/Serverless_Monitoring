"""Groups parsed log records into per-minute buckets (for accurate metric timestamps) and error signatures."""
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

from .logparse import SEVERITY_ORDER, fingerprint

MINUTE_MS = 60_000


@dataclass
class Bucket:
    events: int = 0
    warns: int = 0
    errors: int = 0
    criticals: int = 0
    parse_failures: int = 0
    latencies: list = field(default_factory=list)
    lambda_durations: list = field(default_factory=list)
    lambda_init: list = field(default_factory=list)
    max_memory_mb: Optional[float] = None


@dataclass
class Signature:
    fingerprint: str
    count: int
    severity: str
    sample: str
    request_id: Optional[str]
    trace_id: Optional[str]


def aggregate_by_minute(items):
    """items: iterable of (timestamp_ms, LogRecord). Returns {minute_start_ms: Bucket}. Own output is skipped."""
    buckets = defaultdict(Bucket)
    for ts, rec in items:
        if rec.own_output:
            continue
        b = buckets[int(ts) // MINUTE_MS * MINUTE_MS]
        b.events += 1
        if rec.parse_failed:
            b.parse_failures += 1
        if rec.severity == "WARN":
            b.warns += 1
        elif rec.severity == "ERROR":
            b.errors += 1
        elif rec.severity == "CRITICAL":
            b.criticals += 1
        if rec.latency_ms is not None and rec.latency_ms >= 0:
            b.latencies.append(rec.latency_ms)
        if rec.lambda_duration_ms is not None:
            b.lambda_durations.append(rec.lambda_duration_ms)
        if rec.lambda_init_ms is not None:
            b.lambda_init.append(rec.lambda_init_ms)
        if rec.lambda_memory_mb is not None:
            b.max_memory_mb = max(b.max_memory_mb or 0, rec.lambda_memory_mb)
    return dict(buckets)


def error_signatures(records, limit=5):
    """Most frequent ERROR/CRITICAL messages, with ids and numbers masked so 'the same error' groups together."""
    counts, first, worst = Counter(), {}, {}
    for rec in records:
        if rec.own_output or SEVERITY_ORDER.get(rec.severity, 0) < SEVERITY_ORDER["ERROR"]:
            continue
        fp = fingerprint(rec.message)
        counts[fp] += 1
        first.setdefault(fp, rec)
        if SEVERITY_ORDER[rec.severity] > SEVERITY_ORDER.get(worst.get(fp, "DEBUG"), 0):
            worst[fp] = rec.severity
    out = []
    for fp, count in counts.most_common(limit):
        r = first[fp]
        out.append(Signature(fp, count, worst.get(fp, r.severity), r.message[:300], r.request_id, r.trace_id))
    return out
