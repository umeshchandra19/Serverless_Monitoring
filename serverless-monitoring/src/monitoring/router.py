"""Alert routing: de-duplicate, track incident lifetimes, enrich, notify.

Incident lifecycle for an alarm:
   ALARM  (no open incident)            -> open incident, enrich, notify         ("new")
   ALARM  (flapped back within window)  -> reopen the same incident, notify      ("reopened")
   ALARM  (incident already open)       -> count it, do NOT page again           ("duplicate")
   OK     (incident open)               -> close incident, store duration, notify ("resolved")
The ledger also records notify latency, so response times can be measured from real data (scripts/incident_report.py).
"""
import json
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import alerts as al
from . import emf


@dataclass
class RouterDeps:
    store: object
    publish: Callable                       # (subject, message) -> None   (SNS)
    now: Callable                           # () -> aware datetime (UTC)
    find_logs: Callable = lambda group, pattern, start_ms, limit: []
    webhook: Optional[Callable] = None      # (payload dict) -> None   (Slack/Teams style)
    emit: Callable = emf.emit
    log: Callable = lambda obj: print(json.dumps(obj, default=str))
    config: dict = field(default_factory=dict)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def _parse_iso(text):
    return al.parse_time(text, None)


def handle_event(event, deps):
    source = event.get("source")
    now = deps.now()
    if source == "aws.cloudwatch":
        return handle_alarm(al.parse_alarm_event(event, now), deps)
    if source == "monitoring.logs":
        return handle_log_alert(al.parse_log_alert_event(event, now, deps.config.get("log_alert_severity", "P2")), deps)
    deps.log({"type": "router_ignored", "source": source})
    return {"action": "ignored"}


def _enrich(alert, deps):
    cfg = deps.config
    region = alert.region or cfg.get("region", "")
    sigs, examples = [], []
    try:
        if alert.service:
            sigs = al.top_error_signatures(deps.find_logs, cfg.get("log_ingest_group"), alert.service, deps.now())
        if alert.api:
            examples = al.api_exemplars(deps.find_logs, cfg.get("api_ingest_group"), alert.api, deps.now())
            for e in examples:
                if e.get("trace_id") and region:
                    e["trace_url"] = al.trace_url(region, e["trace_id"])
    except Exception as exc:  # enrichment is a bonus: a failure must never block the page
        deps.log({"type": "enrichment_failed", "error": str(exc)[:200]})
    links = al.console_links(region, cfg.get("dashboard_name"), alert.name if alert.kind == "alarm" else None)
    return sigs, examples, links


def _notify(alert, text, deps, subject_prefix=""):
    deps.publish(al.subject_for(alert, subject_prefix), text)       # must succeed: raise to retry
    if deps.webhook:
        try:
            deps.webhook({"text": text})
        except Exception as exc:
            deps.log({"type": "webhook_failed", "error": str(exc)[:200]})


def _metrics(deps, alert, **values):
    metrics = [(name, v, unit) for name, (v, unit) in values.items()]
    deps.emit(emf.build_records("Monitoring/Alerting", [["Severity"]], {"Severity": alert.severity}, metrics))


def handle_alarm(alert, deps):
    store, now = deps.store, deps.now()
    if alert.state == "INSUFFICIENT_DATA":
        deps.log({"type": "alarm_insufficient_data", "alarm": alert.name})
        return {"action": "ignored_insufficient_data"}
    if alert.state == "OK":
        return _resolve(alert, deps)
    if alert.state != "ALARM":
        return {"action": "ignored"}

    latest = store.latest_incident(alert.name)
    flap_window = deps.config.get("flap_window_seconds", 600)
    kind, incident = "new", None
    if latest and latest["status"] == "OPEN":
        if latest.get("notified_at"):
            store.update_incident(alert.name, latest["opened_at"], {"repeat_count": int(latest.get("repeat_count", 0)) + 1})
            _metrics(deps, alert, Suppressed=(1, "Count"))
            return {"action": "duplicate_suppressed"}
        incident, kind = latest, ("reopened" if latest.get("reopen_count") else "new")  # earlier attempt failed before paging
    elif latest and latest["status"] == "RESOLVED" and latest.get("resolved_at") and \
            (alert.occurred_at - _parse_iso(latest["resolved_at"])).total_seconds() <= flap_window:
        reopen = int(latest.get("reopen_count", 0)) + 1
        store.update_incident(alert.name, latest["opened_at"], {"status": "OPEN", "reopen_count": reopen},
                              remove=("resolved_at", "duration_seconds", "notified_at"))
        incident, kind = dict(latest, status="OPEN", reopen_count=reopen), "reopened"
    if incident is None:
        incident = {"opened_at": _iso(alert.occurred_at), "status": "OPEN", "severity": alert.severity,
                    "service": alert.api or alert.service or "", "summary": alert.summary[:200], "repeat_count": 0,
                    "reopen_count": 0}
        store.put_incident(alert.name, incident)

    sigs, examples, links = _enrich(alert, deps)
    note = "REOPENED: this alarm fired again shortly after recovering." if kind == "reopened" else ""
    text = al.format_alert(alert, signatures=sigs, exemplars=examples, links=links, incident=incident, note=note)
    _notify(alert, text, deps, "REOPENED " if kind == "reopened" else "")
    notified = deps.now()
    latency = max(0, int(round((notified - alert.occurred_at).total_seconds())))
    store.update_incident(alert.name, incident["opened_at"], {"notified_at": _iso(notified), "notify_latency_seconds": latency})
    _metrics(deps, alert, AlertsSent=(1, "Count"), NotifyLatencySeconds=(latency, "Seconds"))
    return {"action": kind, "notify_latency_seconds": latency}


def _resolve(alert, deps):
    latest = deps.store.latest_incident(alert.name)
    if not latest or latest["status"] != "OPEN":
        return {"action": "ignored_no_open_incident"}
    opened = _parse_iso(latest["opened_at"])
    duration = max(0, int((alert.occurred_at - opened).total_seconds()))
    deps.store.update_incident(alert.name, latest["opened_at"],
                               {"status": "RESOLVED", "resolved_at": _iso(alert.occurred_at), "duration_seconds": duration})
    _notify(alert, al.format_recovery(alert, {"duration_seconds": duration}), deps)
    _metrics(deps, alert, IncidentDurationSeconds=(duration, "Seconds"))
    return {"action": "resolved", "duration_seconds": duration}


def handle_log_alert(alert, deps):
    key = f"{alert.service}#{alert.detail.get('fingerprint', 'unknown')}"
    now_epoch = int(deps.now().timestamp())
    if not deps.store.acquire_dedup(key, deps.config.get("log_alert_dedup_seconds", 900), now_epoch):
        _metrics(deps, alert, Suppressed=(1, "Count"))
        return {"action": "duplicate_suppressed"}
    sigs, examples, links = _enrich(alert, deps)
    d = alert.detail
    extra = [f"Log group: {d.get('log_group')}", f"Request:   {d.get('request_id')}" if d.get("request_id") else ""]
    if d.get("trace_id") and alert.region:
        extra.append(f"Trace:     {al.trace_url(alert.region, d['trace_id'])}")
    text = al.format_alert(alert, signatures=sigs, links=links, note="Event-driven alert (no alarm needed): " +
                           f"seen {d.get('count', 1)}x in one log batch.") + "\n" + "\n".join(x for x in extra if x)
    try:
        _notify(alert, text, deps)
    except Exception:
        deps.store.release_dedup(key)   # let the retry page us instead of being suppressed
        raise
    _metrics(deps, alert, AlertsSent=(1, "Count"))
    return {"action": "log_alert_sent"}
