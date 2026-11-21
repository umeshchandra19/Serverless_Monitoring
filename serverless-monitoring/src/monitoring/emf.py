"""CloudWatch Embedded Metric Format (EMF).

Printing one JSON line in this format to a Lambda's stdout makes CloudWatch create custom metrics from it
automatically: no PutMetricData calls, no extra permissions, no extra latency. A metric given as a LIST of values
keeps its distribution, so CloudWatch can compute percentiles (p50/p95/p99) from it.
"""
import json
import math
import sys
import time

MAX_METRICS_PER_RECORD = 100
MAX_VALUES_PER_METRIC = 100
VALID_UNITS = {
    "Seconds", "Microseconds", "Milliseconds", "Bytes", "Kilobytes", "Megabytes", "Gigabytes", "Terabytes",
    "Bits", "Kilobits", "Megabits", "Gigabits", "Terabits", "Percent", "Count", "Bytes/Second",
    "Kilobytes/Second", "Megabytes/Second", "Gigabytes/Second", "Terabytes/Second", "Bits/Second",
    "Kilobits/Second", "Megabits/Second", "Gigabits/Second", "Terabits/Second", "Count/Second", "None",
}


def clean_dimension_value(value):
    """CloudWatch rejects empty dimension values and anything over 1024 characters."""
    text = str(value).strip()
    return (text or "unknown")[:1024]


def _as_list(value):
    values = list(value) if isinstance(value, (list, tuple)) else [value]
    for v in values:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError(f"metric values must be finite numbers, got {v!r}")
    return values


def build_records(namespace, dimension_sets, dimensions, metrics, *, properties=None, timestamp_ms=None):
    """Return a list of EMF dicts (usually one).

    namespace       e.g. "Monitoring/Api"
    dimension_sets  e.g. [["Api"], ["Api", "Route"]]  (each set becomes its own family of metrics)
    dimensions      e.g. {"Api": "orders", "Route": "GET /orders"}
    metrics         list of (name, value-or-list-of-values, unit)
    properties      extra searchable fields stored in the log line but NOT turned into metrics
    Splits into several records when a metric has more than 100 values or there are more than 100 metrics.
    """
    if not namespace or len(namespace) > 256:
        raise ValueError("namespace must be 1-256 characters")
    dims = {k: clean_dimension_value(v) for k, v in dimensions.items()}
    for dim_set in dimension_sets:
        missing = [d for d in dim_set if d not in dims]
        if missing:
            raise ValueError(f"dimension set {dim_set} uses undefined dimensions {missing}")
    properties = properties or {}
    metric_names = {name for name, _, _ in metrics}
    clash = (set(properties) | set(dims)) & metric_names
    if clash or set(properties) & set(dims):
        raise ValueError(f"names used twice in one record: {sorted(clash | (set(properties) & set(dims)))}")

    chunked = []  # [(name, unit, [chunk, chunk, ...])]
    for name, value, unit in metrics:
        if unit not in VALID_UNITS:
            raise ValueError(f"invalid unit {unit!r} for {name}")
        values = _as_list(value)
        if not values:
            continue
        scalar = not isinstance(value, (list, tuple))
        chunks = [values[i:i + MAX_VALUES_PER_METRIC] for i in range(0, len(values), MAX_VALUES_PER_METRIC)]
        chunked.append((name, unit, chunks, scalar))

    records = []
    rounds = max((len(c) for _, _, c, _ in chunked), default=0)
    ts = int(timestamp_ms if timestamp_ms is not None else time.time() * 1000)
    for i in range(rounds):
        in_round = [(n, u, c[i], s) for n, u, c, s in chunked if i < len(c)]
        for start in range(0, len(in_round), MAX_METRICS_PER_RECORD):
            batch = in_round[start:start + MAX_METRICS_PER_RECORD]
            record = {"_aws": {"Timestamp": ts, "CloudWatchMetrics": [{
                "Namespace": namespace,
                "Dimensions": [list(s) for s in dimension_sets],
                "Metrics": [{"Name": n, "Unit": u} for n, u, _, _ in batch]}]}}
            record.update(dims)
            record.update(properties)
            for n, _, values, scalar in batch:
                record[n] = values[0] if scalar and len(values) == 1 else values
            records.append(record)
    return records


def emit(records, out=None):
    """Write records to stdout (one JSON object per line). In Lambda this is all that is needed."""
    out = out or sys.stdout
    for record in records:
        out.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
    out.flush()
