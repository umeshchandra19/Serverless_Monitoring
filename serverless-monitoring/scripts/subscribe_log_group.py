#!/usr/bin/env python3
"""Connect an existing CloudWatch log group to the monitoring pipeline.

    python scripts/subscribe_log_group.py --log-group /ecs/orders --kind app            # application logs
    python scripts/subscribe_log_group.py --log-group /aws/apigateway/orders --kind api # API Gateway access logs
    python scripts/subscribe_log_group.py --log-group /ecs/orders --kind app --remove

Needs the stack deployed (it creates the Lambda permission for CloudWatch Logs). A log group can have at most two
subscription filters in total.
"""
import argparse
import sys

FUNCTION_SUFFIX = {"app": "log-ingest", "api": "api-ingest"}


def subscribe(logs, function_arn, log_group, filter_name, filter_pattern="", dry_run=False):
    existing = logs.describe_subscription_filters(logGroupName=log_group).get("subscriptionFilters", [])
    names = [f["filterName"] for f in existing]
    if filter_name not in names and len(names) >= 2:
        raise SystemExit(f"{log_group} already has 2 subscription filters ({', '.join(names)}); CloudWatch allows at most 2.")
    action = "update" if filter_name in names else "create"
    if not dry_run:
        logs.put_subscription_filter(logGroupName=log_group, filterName=filter_name, filterPattern=filter_pattern, destinationArn=function_arn)
    return action


def unsubscribe(logs, log_group, filter_name, dry_run=False):
    names = [f["filterName"] for f in logs.describe_subscription_filters(logGroupName=log_group).get("subscriptionFilters", [])]
    if filter_name not in names:
        return "absent"
    if not dry_run:
        logs.delete_subscription_filter(logGroupName=log_group, filterName=filter_name)
    return "removed"


def main(argv=None, logs=None, lam=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log-group", required=True)
    ap.add_argument("--kind", choices=sorted(FUNCTION_SUFFIX), required=True, help="app = application logs, api = API Gateway access logs")
    ap.add_argument("--project", default="monitoring")
    ap.add_argument("--filter-pattern", default="", help="CloudWatch Logs filter pattern; empty = every line")
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if logs is None or lam is None:
        import boto3
        logs, lam = boto3.client("logs"), boto3.client("lambda")
    filter_name = f"{a.project}-{a.kind}"
    if a.remove:
        print(f"{a.log_group}: {unsubscribe(logs, a.log_group, filter_name, a.dry_run)}")
        return 0
    arn = lam.get_function(FunctionName=f"{a.project}-{FUNCTION_SUFFIX[a.kind]}")["Configuration"]["FunctionArn"]
    print(f"{a.log_group}: {subscribe(logs, arn, a.log_group, filter_name, a.filter_pattern, a.dry_run)}{' (dry run)' if a.dry_run else ''} -> {arn}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
