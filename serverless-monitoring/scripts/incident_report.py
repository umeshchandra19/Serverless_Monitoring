#!/usr/bin/env python3
"""Measure incident response from the incident ledger, and compare it with a baseline.

    python scripts/incident_report.py --table monitoring-incidents                       # read DynamoDB
    python scripts/incident_report.py --from-json incidents.json                         # or an export
    python scripts/incident_report.py --table monitoring-incidents --baseline-csv before.csv --since 2026-09-01

The baseline CSV is YOUR data from before the pipeline (ticket system, spreadsheet, chat history) with columns
  opened_at,resolved_at          (ISO 8601 timestamps, one row per incident)
What is measured:
  MTTR            time from alarm to recovery (opened_at -> resolved_at)
  Time to notify  time from the alarm firing to the page being delivered (notify_latency_seconds)
Be careful with before/after comparisons: they only mean something if the incidents are comparable (same systems,
similar severity, enough of them). The report warns you when there are fewer than 10 incidents on either side.
"""
import argparse
import csv
import json
import statistics
import sys
from datetime import datetime, timezone


def parse_ts(value):
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def percentile(values, p):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p))] if ordered else None


def summarize(incidents, since=None, until=None):
    rows = []
    for i in incidents:
        opened = parse_ts(i["opened_at"])
        if (since and opened < since) or (until and opened >= until):
            continue
        rows.append(i)
    resolved = [i for i in rows if i.get("status") == "RESOLVED" and i.get("duration_seconds") is not None]
    durations = [float(i["duration_seconds"]) for i in resolved]
    notify = [float(i["notify_latency_seconds"]) for i in rows if i.get("notify_latency_seconds") is not None]
    by_sev = {}
    for i in rows:
        by_sev[i.get("severity", "?")] = by_sev.get(i.get("severity", "?"), 0) + 1
    return {"incidents": len(rows), "resolved": len(resolved), "open": sum(1 for i in rows if i.get("status") == "OPEN"),
            "flapping": sum(1 for i in rows if int(i.get("reopen_count", 0)) > 0), "by_severity": by_sev,
            "mttr_mean_s": statistics.mean(durations) if durations else None,
            "mttr_median_s": statistics.median(durations) if durations else None,
            "mttr_p90_s": percentile(durations, 0.9),
            "notify_mean_s": statistics.mean(notify) if notify else None, "notify_max_s": max(notify) if notify else None}


def load_baseline(path):
    out = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            opened, resolved = parse_ts(row["opened_at"]), parse_ts(row["resolved_at"])
            out.append({"opened_at": row["opened_at"], "status": "RESOLVED", "duration_seconds": (resolved - opened).total_seconds()})
    return out


def fmt(seconds):
    if seconds is None:
        return "n/a"
    seconds = int(round(seconds))
    return f"{seconds // 3600}h {seconds % 3600 // 60}m" if seconds >= 3600 else f"{seconds // 60}m {seconds % 60}s"


def render(current, baseline=None):
    lines = ["Incident report", "================",
             f"Incidents: {current['incidents']} ({current['resolved']} resolved, {current['open']} open, {current['flapping']} flapped)",
             f"By severity: {json.dumps(current['by_severity'], sort_keys=True)}",
             f"MTTR  mean {fmt(current['mttr_mean_s'])} | median {fmt(current['mttr_median_s'])} | p90 {fmt(current['mttr_p90_s'])}",
             f"Time to notify  mean {fmt(current['notify_mean_s'])} | max {fmt(current['notify_max_s'])}"]
    if baseline is not None:
        lines += ["", f"Baseline (before): {baseline['resolved']} resolved incidents",
                  f"MTTR  mean {fmt(baseline['mttr_mean_s'])} | median {fmt(baseline['mttr_median_s'])} | p90 {fmt(baseline['mttr_p90_s'])}", ""]
        for label, key in (("mean", "mttr_mean_s"), ("median", "mttr_median_s")):
            if baseline[key] and current[key] is not None:
                change = (current[key] - baseline[key]) / baseline[key] * 100
                lines.append(f"MTTR {label}: {change:+.0f}% vs baseline ({'faster' if change < 0 else 'slower'})")
        if min(baseline["resolved"], current["resolved"]) < 10:
            lines.append("WARNING: fewer than 10 resolved incidents on one side. Treat this as indicative, not proof.")
    return "\n".join(lines)


def read_table(name):
    import boto3
    from monitoring.store import DynamoStore
    return DynamoStore(boto3.resource("dynamodb").Table(name)).all_incidents()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--table")
    src.add_argument("--from-json")
    ap.add_argument("--baseline-csv")
    ap.add_argument("--since", help="only incidents opened on/after this date (YYYY-MM-DD)")
    ap.add_argument("--until", help="only incidents opened before this date")
    a = ap.parse_args(argv)
    if a.table:
        sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0] + "/src")
        incidents = read_table(a.table)
    else:
        with open(a.from_json) as f:
            incidents = json.load(f)
    since = parse_ts(a.since) if a.since else None
    until = parse_ts(a.until) if a.until else None
    baseline = summarize(load_baseline(a.baseline_csv)) if a.baseline_csv else None
    print(render(summarize(incidents, since, until), baseline))
    return 0


if __name__ == "__main__":
    sys.exit(main())
