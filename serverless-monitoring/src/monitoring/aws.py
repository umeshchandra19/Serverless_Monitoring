"""Thin, lazy wrappers around boto3 (imported only when used, so unit tests need no AWS libraries)."""
import json
import os


def _boto3():
    import boto3  # available in the Lambda runtime
    return boto3


def put_events_client():
    client = _boto3().client("events")

    def put_events(entries):
        """Sends EventBridge entries in batches of 10. Returns the number of entries that failed."""
        failed = 0
        for i in range(0, len(entries), 10):
            response = client.put_events(Entries=entries[i:i + 10])
            failed += response.get("FailedEntryCount", 0)
        return failed
    return put_events


def sns_publisher(topic_arn):
    client = _boto3().client("sns")

    def publish(subject, message):
        client.publish(TopicArn=topic_arn, Subject=subject[:100], Message=message)
    return publish


def logs_client():
    return _boto3().client("logs")


def incident_table(name):
    return _boto3().resource("dynamodb").Table(name)


def env_json(name, default):
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default
