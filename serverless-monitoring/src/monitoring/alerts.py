"""Turns raw alarm / log-alert events into rich, actionable notifications."""
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

SEVERITIES = ("P1", "P2", "P3", "P4")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.:/\-]{1,128}$")
_SEV = re.compile(r"severity\s*=\s*(P[1-4])", re.I)
_RUNBOOK = re.compile(r"runbook\s*=\s*(https?://[^\s;,]+)", re.I)
_KEYVAL = re.compile(r"\b(severity|runbook)\s*=\s*\S+;?", re.I)


@dataclass
class Alert:
    kind: str                       # "alarm" or "log"
    name: str
    state: str                      # ALARM / OK / INSUFFICIENT_DATA / LOG
    severity: str
    occurred_at: datetime
    summary: str = ""
    reason: str = ""
    service: Optional[str] = None
    api: Optional[str] = None
    runbook: Optional[str] = None
    dimensions: dict = field(default_factory=dict)
    detail: dict = field(default_factory=dict)
    region: str = ""


def parse_time(value, fallback):
    if not value:
        return fallback
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(value, fmt).astimezone(timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return fallback


def parse_description(text):
    """Alarm descriptions follow 'severity=P1; runbook=https://...; free text'."""
    text = text or ""
    sev = _SEV.search(text)
    runbook = _RUNBOOK.search(text)
    summary = re.sub(r"\s+", " ", _KEYVAL.sub("", text)).strip(" ;")
    return (sev.group(1).upper() if sev else "P3"), (runbook.group(1) if runbook else None), summary


def parse_alarm_event(event, now):
    detail = event["detail"]
    state = detail["state"]["value"]
    severity, runbook, summary = parse_description(detail.get("configuration", {}).get("description", ""))
    dims = {}
    for m in detail.get("configuration", {}).get("metrics", []):
        dims.update(m.get("metricStat", {}).get("metric", {}).get("dimensions", {}) or {})
    return Alert(kind="alarm", name=detail["alarmName"], state=state, severity=severity,
                 occurred_at=parse_time(detail["state"].get("timestamp"), parse_time(event.get("time"), now)),
                 summary=summary, reason=detail["state"].get("reason", ""), service=dims.get("Service"),
                 api=dims.get("Api"), runbook=runbook, dimensions=dims, region=event.get("region", ""))


def parse_log_alert_event(event, now, severity="P2"):
    d = event["detail"]
    if isinstance(d, str):
        d = json.loads(d)
    service = d.get("service", "unknown")
    return Alert(kind="log", name=f"log-alert:{service}", state="LOG", severity=severity,
                 occurred_at=parse_time(event.get("time"), now), summary=f"Critical log event in {service}: {d.get('reason', '')}",
                 reason=d.get("sample", ""), service=service, dimensions={"Service": service}, detail=d,
                 region=event.get("region", ""))


# ---------- enrichment (best effort, never fatal) ----------

def safe_name(name):
    return bool(name and _SAFE_NAME.match(name))


def top_error_signatures(find_logs, log_group, service, now, minutes=15, limit=3):
    if not (log_group and safe_name(service)):
        return []
    start_ms = int((now - timedelta(minutes=minutes)).timestamp() * 1000)
    pattern = f'{{ $.type = "error_signature" && $.service = "{service}" }}'
    merged = defaultdict(lambda: {"count": 0})
    for line in find_logs(log_group, pattern, start_ms, 50):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        m = merged[d.get("fingerprint", "?")]
        m["count"] += int(d.get("count", 1))
        m.setdefault("sample", d.get("sample", ""))
        m.setdefault("trace_id", d.get("trace_id"))
        m.setdefault("fingerprint", d.get("fingerprint"))
    return sorted(merged.values(), key=lambda m: -m["count"])[:limit]


def api_exemplars(find_logs, log_group, api, now, minutes=15, limit=3):
    if not (log_group and safe_name(api)):
        return []
    start_ms = int((now - timedelta(minutes=minutes)).timestamp() * 1000)
    pattern = f'{{ $.type = "api_exemplar" && $.api = "{api}" }}'
    found = []
    for line in find_logs(log_group, pattern, start_ms, 50):
        try:
            found.append(json.loads(line))
        except ValueError:
            continue
    found.sort(key=lambda d: (d.get("reason") != "5xx", -(d.get("latency_ms") or 0)))
    return found[:limit]


# ---------- formatting ----------

def console_links(region, dashboard, alarm_name, trace_ids=()):
    base = f"https://{region}.console.aws.amazon.com"
    links = {}
    if dashboard and region:
        links["Dashboard"] = f"{base}/cloudwatch/home?region={region}#dashboards:name={dashboard}"
    if alarm_name and region:
        links["Alarm"] = f"{base}/cloudwatch/home?region={region}#alarmsV2:alarm/{alarm_name}"
    return links


def trace_url(region, trace_id):
    return f"https://{region}.console.aws.amazon.com/xray/home?region={region}#/traces/{trace_id}"


def subject_for(alert, prefix=""):
    return f"[{alert.severity}] {prefix}{alert.state}: {alert.name}"[:100]


def format_alert(alert, *, signatures=(), exemplars=(), links=None, incident=None, note=""):
    lines = [f"[{alert.severity}] {alert.state}: {alert.name}"]
    if note:
        lines.append(note)
    target = alert.api or alert.service
    if target:
        lines.append(f"Target:   {target}")
    if alert.summary:
        lines.append(f"What:     {alert.summary}")
    if alert.reason:
        lines.append(f"Why:      {alert.reason[:400]}")
    lines.append(f"Since:    {alert.occurred_at.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    if incident and incident.get("reopen_count"):
        lines.append(f"Flapping: reopened {incident['reopen_count']} time(s) within the flap window")
    if alert.runbook:
        lines.append(f"Runbook:  {alert.runbook}")
    for label, url in (links or {}).items():
        lines.append(f"{label + ':':<9} {url}")
    if signatures:
        lines.append("")
        lines.append("Top errors (last 15 min):")
        for s in signatures:
            lines.append(f"  - {s['count']}x  {s.get('sample', '')[:160]}")
    if exemplars:
        lines.append("")
        lines.append("Example failing/slow requests:")
        for e in exemplars:
            lat = f"{e['latency_ms']:.0f} ms" if e.get("latency_ms") is not None else "n/a"
            lines.append(f"  - {e.get('status')} {e.get('route')}  {lat}  ({e.get('reason')})  {e.get('trace_url') or ''}".rstrip())
    return "\n".join(lines)


def format_recovery(alert, incident):
    mins = (incident or {}).get("duration_seconds")
    took = f" after {int(mins) // 60} min {int(mins) % 60} s" if mins is not None else ""
    return (f"[{alert.severity}] OK: {alert.name}\nRecovered{took}.\n"
            f"At: {alert.occurred_at.strftime('%Y-%m-%d %H:%M:%S')} UTC")
