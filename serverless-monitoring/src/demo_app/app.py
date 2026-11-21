"""A tiny API to monitor. It logs JSON, and fails or slows down on demand so you can watch the pipeline react.

   GET  /health
   GET  /orders[?fail=1][&slow=800]      fail=1 returns a 500, slow=N adds N ms (max 3000)
   GET  /orders/{id}
   POST /orders
Set FAILURE_RATE (0-1) to make a share of requests fail randomly.
"""
import json
import os
import random
import time

ORDERS = {str(i): {"id": str(i), "item": f"item-{i}", "qty": i % 5 + 1} for i in range(1, 6)}


def _log(level, message, **fields):
    print(json.dumps({"level": level, "message": message, **fields}, default=str))


def _response(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}


def handler(event, context):
    started = time.perf_counter()
    qs = event.get("queryStringParameters") or {}
    route = f"{event.get('httpMethod')} {event.get('resource')}"
    request_id = getattr(context, "aws_request_id", "local")
    trace_id = os.environ.get("_X_AMZN_TRACE_ID", "").split(";")[0].replace("Root=", "") or None
    ctx = {"request_id": request_id, "trace_id": trace_id, "route": route}

    slow_ms = min(int(qs.get("slow", "0") or 0), 3000)
    if slow_ms:
        time.sleep(slow_ms / 1000)
    random_failure = random.random() < float(os.environ.get("FAILURE_RATE", "0.02"))

    if event.get("resource") == "/health":
        return _response(200, {"status": "ok"})
    if qs.get("fail") == "1" or random_failure:
        _log("error", "database timeout while loading orders", error_type="DatabaseTimeout",
             latency_ms=round((time.perf_counter() - started) * 1000, 1), **ctx)
        return _response(500, {"error": "internal error"})

    method, order_id = event.get("httpMethod"), (event.get("pathParameters") or {}).get("id")
    if method == "GET" and order_id:
        order = ORDERS.get(order_id)
        status, body = (200, order) if order else (404, {"error": "not found"})
    elif method == "POST":
        status, body = 201, {"id": str(random.randint(100, 999)), "created": True}
    else:
        status, body = 200, {"orders": list(ORDERS.values())}
    _log("info" if status < 400 else "warn", "request served", status=status,
         latency_ms=round((time.perf_counter() - started) * 1000, 1), **ctx)
    return _response(status, body)
