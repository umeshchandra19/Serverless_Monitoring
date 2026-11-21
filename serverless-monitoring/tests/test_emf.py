import io
import json
import unittest

from tests import helpers  # noqa: F401
from monitoring import emf


class EmfTests(unittest.TestCase):
    def test_structure_matches_the_embedded_metric_format(self):
        [r] = emf.build_records("NS", [["Service"]], {"Service": "orders"},
                                [("Errors", 3, "Count"), ("Latency", [10.0, 20.0], "Milliseconds")], timestamp_ms=1700000000000)
        cw = r["_aws"]["CloudWatchMetrics"][0]
        self.assertEqual(r["_aws"]["Timestamp"], 1700000000000)
        self.assertEqual(cw["Namespace"], "NS")
        self.assertEqual(cw["Dimensions"], [["Service"]])
        self.assertEqual(cw["Metrics"], [{"Name": "Errors", "Unit": "Count"}, {"Name": "Latency", "Unit": "Milliseconds"}])
        self.assertEqual((r["Service"], r["Errors"], r["Latency"]), ("orders", 3, [10.0, 20.0]))

    def test_large_distributions_are_split_into_100_value_records(self):
        recs = emf.build_records("NS", [["S"]], {"S": "x"}, [("Latency", list(range(250)), "Milliseconds"), ("N", 250, "Count")], timestamp_ms=1)
        self.assertEqual([len(r["Latency"]) for r in recs if "Latency" in r], [100, 100, 50])
        self.assertEqual(sum(len(r["Latency"]) for r in recs), 250)
        self.assertEqual(sum(1 for r in recs if "N" in r), 1)  # scalar metric is not duplicated

    def test_more_than_100_metrics_split_across_records(self):
        recs = emf.build_records("NS", [["S"]], {"S": "x"}, [(f"m{i}", i, "Count") for i in range(130)], timestamp_ms=1)
        self.assertEqual([len(r["_aws"]["CloudWatchMetrics"][0]["Metrics"]) for r in recs], [100, 30])

    def test_empty_metric_lists_are_skipped(self):
        [r] = emf.build_records("NS", [["S"]], {"S": "x"}, [("Latency", [], "Milliseconds"), ("N", 1, "Count")], timestamp_ms=1)
        self.assertNotIn("Latency", r)

    def test_rejects_bad_input(self):
        for bad in (float("nan"), float("inf"), "12", True):
            with self.assertRaises(ValueError):
                emf.build_records("NS", [["S"]], {"S": "x"}, [("M", bad, "Count")])
        with self.assertRaises(ValueError):
            emf.build_records("NS", [["Missing"]], {"S": "x"}, [("M", 1, "Count")])
        with self.assertRaises(ValueError):
            emf.build_records("NS", [["S"]], {"S": "x"}, [("M", 1, "Furlongs")])
        with self.assertRaises(ValueError):
            emf.build_records("NS", [["S"]], {"S": "x"}, [("S", 1, "Count")])  # metric name collides with a dimension

    def test_empty_dimension_values_are_made_valid(self):
        [r] = emf.build_records("NS", [["S"]], {"S": "  "}, [("M", 1, "Count")], timestamp_ms=1)
        self.assertEqual(r["S"], "unknown")

    def test_emit_writes_one_json_line_per_record(self):
        buf = io.StringIO()
        emf.emit(emf.build_records("NS", [["S"]], {"S": "x"}, [("M", 1, "Count")], timestamp_ms=1) * 2, out=buf)
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["M"], 1)


if __name__ == "__main__":
    unittest.main()
