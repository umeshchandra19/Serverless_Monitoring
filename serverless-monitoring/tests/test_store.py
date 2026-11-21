import sys
import types
import unittest

from tests import helpers  # noqa: F401
from monitoring.store import DynamoStore, InMemoryStore


class RecordingTable:
    def __init__(self):
        self.calls, self.query_items, self.scan_pages, self.raise_on_put = [], [], [], None

    def query(self, **kw):
        self.calls.append(("query", kw))
        return {"Items": self.query_items}

    def put_item(self, **kw):
        self.calls.append(("put_item", kw))
        if self.raise_on_put:
            raise self.raise_on_put

    def update_item(self, **kw):
        self.calls.append(("update_item", kw))

    def delete_item(self, **kw):
        self.calls.append(("delete_item", kw))

    def scan(self, **kw):
        self.calls.append(("scan", kw))
        return self.scan_pages.pop(0)


class DynamoStoreTests(unittest.TestCase):
    def setUp(self):
        self.t = RecordingTable()
        self.s = DynamoStore(self.t)

    def test_latest_incident_queries_newest_first(self):
        self.t.query_items = [{"status": "OPEN"}]
        self.assertEqual(self.s.latest_incident("a"), {"status": "OPEN"})
        kw = self.t.calls[0][1]
        self.assertEqual((kw["ScanIndexForward"], kw["Limit"]), (False, 1))
        self.assertEqual(kw["ExpressionAttributeValues"][":pk"], "ALARM#a")
        self.t.query_items = []
        self.assertIsNone(self.s.latest_incident("a"))

    def test_put_incident_adds_keys(self):
        self.s.put_incident("a", {"opened_at": "2026-01-01T00:00:00", "status": "OPEN"})
        item = self.t.calls[0][1]["Item"]
        self.assertEqual((item["pk"], item["sk"]), ("ALARM#a", "INCIDENT#2026-01-01T00:00:00"))

    def test_update_builds_set_and_remove_expression(self):
        self.s.update_incident("a", "t0", {"status": "OPEN", "reopen_count": 2}, remove=("resolved_at", "duration_seconds"))
        kw = self.t.calls[0][1]
        self.assertEqual(kw["Key"], {"pk": "ALARM#a", "sk": "INCIDENT#t0"})
        self.assertEqual(kw["UpdateExpression"], "SET #s0 = :s0, #s1 = :s1 REMOVE #r0, #r1")
        self.assertEqual(kw["ExpressionAttributeNames"], {"#s0": "status", "#s1": "reopen_count", "#r0": "resolved_at", "#r1": "duration_seconds"})
        self.assertEqual(kw["ExpressionAttributeValues"], {":s0": "OPEN", ":s1": 2})

    def test_update_with_only_remove_has_no_values(self):
        self.s.update_incident("a", "t0", {}, remove=("x",))
        self.assertNotIn("ExpressionAttributeValues", self.t.calls[0][1])

    def test_dedup_acquire_uses_a_condition_and_ttl(self):
        self._with_botocore()
        self.assertTrue(self.s.acquire_dedup("k", 900, 1000))
        kw = self.t.calls[0][1]
        self.assertEqual(kw["Item"]["expires_at"], 1900)
        self.assertGreater(kw["Item"]["ttl"], 1900)
        self.assertIn("attribute_not_exists(pk)", kw["ConditionExpression"])

    def _with_botocore(self):
        class ClientError(Exception):
            def __init__(self, code):
                self.response = {"Error": {"Code": code}}
        mod = types.ModuleType("botocore.exceptions"); mod.ClientError = ClientError
        sys.modules["botocore"] = types.ModuleType("botocore"); sys.modules["botocore.exceptions"] = mod
        self.ClientError = ClientError
        return True

    def test_dedup_conflict_means_suppressed_and_other_errors_propagate(self):
        self._with_botocore()
        self.t.raise_on_put = self.ClientError("ConditionalCheckFailedException")
        self.assertFalse(self.s.acquire_dedup("k", 900, 1000))
        self.t.raise_on_put = self.ClientError("ProvisionedThroughputExceededException")
        with self.assertRaises(self.ClientError):
            self.s.acquire_dedup("k", 900, 1000)

    def test_scan_follows_pagination(self):
        self.t.scan_pages = [{"Items": [{"a": 1}], "LastEvaluatedKey": {"pk": "x"}}, {"Items": [{"a": 2}]}]
        self.assertEqual(self.s.all_incidents(), [{"a": 1}, {"a": 2}])
        self.assertEqual(self.t.calls[1][1]["ExclusiveStartKey"], {"pk": "x"})


class InMemoryStoreTests(unittest.TestCase):
    def test_latest_is_the_most_recent_incident(self):
        s = InMemoryStore()
        s.put_incident("a", {"opened_at": "2026-01-01T00:00:00", "status": "RESOLVED"})
        s.put_incident("a", {"opened_at": "2026-02-01T00:00:00", "status": "OPEN"})
        self.assertEqual(s.latest_incident("a")["status"], "OPEN")
        self.assertIsNone(s.latest_incident("other"))


if __name__ == "__main__":
    unittest.main()
