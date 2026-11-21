import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from monitoring.cwlogs import encode_subscription_event  # noqa: E402
from monitoring.ingest import IngestDeps  # noqa: E402

T0 = datetime(2026, 11, 19, 10, 0, 0, tzinfo=timezone.utc)
T0_MS = int(T0.timestamp() * 1000)


class Capture:
    """Collects everything a handler would write to stdout / EventBridge."""

    def __init__(self, **config):
        self.emitted, self.logs, self.events = [], [], []
        self.fail_put_events = False

        def put_events(entries):
            if self.fail_put_events:
                raise RuntimeError("events unavailable")
            self.events.extend(entries)
            return 0
        self.deps = IngestDeps(emit=lambda recs: self.emitted.extend(recs), log=self.logs.append, put_events=put_events,
                               now_ms=lambda: T0_MS, config=config)

    def metric(self, name, without=(), **dims):
        """All values of a metric across emitted records whose dimensions match (and lack the `without` keys)."""
        out = []
        for r in self.emitted:
            if name in r and all(r.get(k) == v for k, v in dims.items()) and not any(k in r for k in without):
                out.append((r["_aws"]["Timestamp"], r[name]))
        return sorted(out, key=lambda x: x[0])


def logs_event(log_group, messages, timestamps=None):
    return encode_subscription_event(log_group, "stream-1", messages, timestamps)


def alarm_event(name="monitoring-svc-api5xx-orders", state="ALARM", at="2026-11-19T10:00:00.000+0000",
                description="severity=P1; runbook=https://wiki.example/runbooks/5xx; API 5xx rate above 5%",
                dims=None, reason="Threshold Crossed: 1 datapoint [12.0] was greater than the threshold (5.0)."):
    dims = dims if dims is not None else {"Api": "orders"}
    return {"version": "0", "id": "abc", "detail-type": "CloudWatch Alarm State Change", "source": "aws.cloudwatch",
            "account": "123456789012", "time": "2026-11-19T10:00:05Z", "region": "ap-south-1",
            "resources": [f"arn:aws:cloudwatch:ap-south-1:123456789012:alarm:{name}"],
            "detail": {"alarmName": name, "state": {"value": state, "reason": reason, "timestamp": at},
                       "previousState": {"value": "OK" if state == "ALARM" else "ALARM", "reason": "", "timestamp": at},
                       "configuration": {"description": description,
                                         "metrics": [{"id": "m1", "metricStat": {"metric": {
                                             "namespace": "Monitoring/Api", "name": "Http5xx", "dimensions": dims},
                                             "period": 60, "stat": "Sum"}, "returnData": True}]}}}
