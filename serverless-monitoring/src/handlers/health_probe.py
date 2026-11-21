"""Lambda (scheduled every minute): synthetic health checks of your endpoints.

Targets come from the PROBE_TARGETS environment variable (JSON list), for example
  [{"name": "orders-health", "url": "https://api.example.com/health", "expect_status": 200,
    "timeout_s": 5, "max_latency_ms": 1500}]
For each target it emits (EMF, Monitoring/Probes, dimension Target):
  Availability (1 = healthy, 0 = down or too slow), ResponseTimeMs, StatusCode, CertDaysRemaining (https only)
"""
import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from monitoring import emf

NAMESPACE = os.environ.get("PROBE_METRIC_NAMESPACE", "Monitoring/Probes")


def validate_targets(targets):
    names = set()
    for t in targets:
        if not isinstance(t, dict) or not t.get("name") or not str(t.get("url", "")).startswith(("http://", "https://")):
            raise ValueError(f"invalid probe target: {t!r}")
        if t["name"] in names:
            raise ValueError(f"duplicate probe target name: {t['name']}")
        names.add(t["name"])
    return targets


def http_get(url, timeout):
    """Returns (status_code, elapsed_ms, error_or_None). HTTP error statuses are results, not exceptions."""
    request = urllib.request.Request(url, headers={"User-Agent": "monitoring-probe/1.0"})
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            resp.read(65536)
            return resp.status, (time.perf_counter() - start) * 1000, None
    except urllib.error.HTTPError as exc:
        return exc.code, (time.perf_counter() - start) * 1000, None
    except (urllib.error.URLError, socket.timeout, TimeoutError, ssl.SSLError, ConnectionError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return None, (time.perf_counter() - start) * 1000, f"{type(reason).__name__}: {reason}"[:200]


def cert_days_remaining(host, port, timeout, now=None):
    """Days until the server certificate expires, or None if it cannot be read."""
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                return days_until(tls.getpeercert()["notAfter"], now)
    except (OSError, ssl.SSLError, KeyError, ValueError):
        return None


def days_until(not_after, now=None):
    return (ssl.cert_time_to_seconds(not_after) - (now if now is not None else time.time())) / 86400


def probe_one(target, fetch=http_get, cert_check=cert_days_remaining):
    timeout = float(target.get("timeout_s", 5))
    status, elapsed, error = fetch(target["url"], timeout)
    expected = int(target.get("expect_status", 200))
    max_latency = float(target.get("max_latency_ms", 0)) or None
    healthy = status == expected and (max_latency is None or elapsed <= max_latency)
    result = {"name": target["name"], "healthy": healthy, "status": status, "elapsed_ms": round(elapsed, 1), "error": error}
    if error is None and status != expected:
        result["error"] = f"unexpected status {status}"
    elif healthy is False and error is None and status == expected:
        result["error"] = f"too slow: {elapsed:.0f} ms > {max_latency:.0f} ms"
    parsed = urlparse(target["url"])
    if parsed.scheme == "https" and target.get("check_certificate", True):
        result["cert_days"] = cert_check(parsed.hostname, parsed.port or 443, timeout)
    return result


def process(targets, emit=emf.emit, fetch=http_get, cert_check=cert_days_remaining, now_ms=None):
    validate_targets(targets)
    if not targets:
        return []
    with ThreadPoolExecutor(max_workers=min(10, len(targets))) as pool:
        results = list(pool.map(lambda t: probe_one(t, fetch, cert_check), targets))
    ts = now_ms if now_ms is not None else int(time.time() * 1000)
    for r in results:
        metrics = [("Availability", 1 if r["healthy"] else 0, "None"), ("ResponseTimeMs", r["elapsed_ms"], "Milliseconds")]
        if r["status"] is not None:
            metrics.append(("StatusCode", r["status"], "None"))
        if r.get("cert_days") is not None:
            metrics.append(("CertDaysRemaining", round(r["cert_days"], 1), "None"))
        props = {"probe_error": r["error"]} if r["error"] else {}
        emit(emf.build_records(NAMESPACE, [["Target"]], {"Target": r["name"]}, metrics, properties=props, timestamp_ms=ts))
    return results


def handler(event, context):
    targets = json.loads(os.environ.get("PROBE_TARGETS", "[]"))
    results = process(targets)
    return {"probed": len(results), "down": [r["name"] for r in results if not r["healthy"]]}
