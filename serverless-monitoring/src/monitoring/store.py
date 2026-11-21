"""Incident ledger + alert de-duplication.

One DynamoDB table (pk, sk, TTL on 'ttl'):
  incident   pk = ALARM#<alarm name>   sk = INCIDENT#<opened_at ISO>    (status OPEN / RESOLVED, durations, ...)
  dedup      pk = DEDUP#<key>          sk = LOG                         (suppresses repeats of the same log alert)
InMemoryStore has the same interface, for unit tests and the local simulator.
"""
import copy


class InMemoryStore:
    def __init__(self):
        self.items = {}

    # ---- incidents ----
    def latest_incident(self, alarm_name):
        rows = [v for (pk, sk), v in self.items.items() if pk == f"ALARM#{alarm_name}" and sk.startswith("INCIDENT#")]
        rows.sort(key=lambda r: r["opened_at"])
        return copy.deepcopy(rows[-1]) if rows else None

    def put_incident(self, alarm_name, item):
        self.items[(f"ALARM#{alarm_name}", f"INCIDENT#{item['opened_at']}")] = dict(item, alarm_name=alarm_name)

    def update_incident(self, alarm_name, opened_at, updates, remove=()):
        row = self.items[(f"ALARM#{alarm_name}", f"INCIDENT#{opened_at}")]
        row.update(updates)
        for key in remove:
            row.pop(key, None)

    def all_incidents(self):
        return [copy.deepcopy(v) for (pk, sk), v in self.items.items() if sk.startswith("INCIDENT#")]

    # ---- dedup ----
    def acquire_dedup(self, key, ttl_seconds, now_epoch):
        existing = self.items.get((f"DEDUP#{key}", "LOG"))
        if existing and existing["expires_at"] > now_epoch:
            return False
        self.items[(f"DEDUP#{key}", "LOG")] = {"expires_at": now_epoch + ttl_seconds}
        return True

    def release_dedup(self, key):
        self.items.pop((f"DEDUP#{key}", "LOG"), None)


class DynamoStore:
    """Same interface on a boto3 DynamoDB Table resource."""

    def __init__(self, table):
        self.table = table

    def latest_incident(self, alarm_name):
        resp = self.table.query(KeyConditionExpression="pk = :pk AND begins_with(sk, :p)",
                                ExpressionAttributeValues={":pk": f"ALARM#{alarm_name}", ":p": "INCIDENT#"},
                                ScanIndexForward=False, Limit=1)
        items = resp.get("Items", [])
        return items[0] if items else None

    def put_incident(self, alarm_name, item):
        self.table.put_item(Item=dict(item, pk=f"ALARM#{alarm_name}", sk=f"INCIDENT#{item['opened_at']}", alarm_name=alarm_name))

    def update_incident(self, alarm_name, opened_at, updates, remove=()):
        names, values, sets = {}, {}, []
        for i, (k, v) in enumerate(updates.items()):
            names[f"#s{i}"], values[f":s{i}"] = k, v
            sets.append(f"#s{i} = :s{i}")
        expr = "SET " + ", ".join(sets) if sets else ""
        if remove:
            removes = []
            for i, k in enumerate(remove):
                names[f"#r{i}"] = k
                removes.append(f"#r{i}")
            expr += (" " if expr else "") + "REMOVE " + ", ".join(removes)
        kwargs = {"Key": {"pk": f"ALARM#{alarm_name}", "sk": f"INCIDENT#{opened_at}"}, "UpdateExpression": expr,
                  "ExpressionAttributeNames": names}
        if values:
            kwargs["ExpressionAttributeValues"] = values
        self.table.update_item(**kwargs)

    def all_incidents(self):
        items, kwargs = [], {"FilterExpression": "begins_with(sk, :p)", "ExpressionAttributeValues": {":p": "INCIDENT#"}}
        while True:
            resp = self.table.scan(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                return items
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def acquire_dedup(self, key, ttl_seconds, now_epoch):
        from botocore.exceptions import ClientError
        try:
            self.table.put_item(Item={"pk": f"DEDUP#{key}", "sk": "LOG", "expires_at": now_epoch + ttl_seconds,
                                      "ttl": now_epoch + ttl_seconds + 86400},
                                ConditionExpression="attribute_not_exists(pk) OR expires_at < :now",
                                ExpressionAttributeValues={":now": now_epoch})
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def release_dedup(self, key):
        self.table.delete_item(Key={"pk": f"DEDUP#{key}", "sk": "LOG"})
