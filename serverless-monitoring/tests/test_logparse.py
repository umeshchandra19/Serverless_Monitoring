import unittest

from tests import helpers  # noqa: F401
from monitoring import logparse as lp
from monitoring.aggregate import aggregate_by_minute, error_signatures


class ParseTests(unittest.TestCase):
    def test_json_log_with_fields(self):
        r = lp.parse_line('{"level":"error","msg":"db timeout for order 4812","latency_ms":1200,"request_id":"r-1","trace_id":"1-abc"}')
        self.assertEqual((r.severity, r.latency_ms, r.request_id, r.trace_id), ("ERROR", 1200.0, "r-1", "1-abc"))
        self.assertTrue(r.is_json)

    def test_severity_aliases(self):
        for raw, want in (("warning", "WARN"), ("FATAL", "CRITICAL"), ("Err", "ERROR"), ("notice", "INFO"), ("trace", "DEBUG")):
            self.assertEqual(lp.parse_line('{"level":"%s","msg":"x"}' % raw).severity, want)

    def test_json_without_level_but_with_exception_is_an_error(self):
        self.assertEqual(lp.parse_line('{"message":"boom","exception":"KeyError"}').severity, "ERROR")
        self.assertEqual(lp.parse_line('{"message":"hello"}').severity, "INFO")

    def test_error_type_is_prefixed_to_the_message(self):
        self.assertEqual(lp.parse_line('{"level":"error","message":"no route","error_type":"DbError"}').message, "DbError: no route")

    def test_plain_text_levels(self):
        self.assertEqual(lp.parse_line("[ERROR]\t2026-01-01T00:00:00Z\tabc\tit broke").severity, "ERROR")
        self.assertEqual(lp.parse_line("2026-01-01 12:00:00 WARNING disk at 91%").severity, "WARN")
        self.assertEqual(lp.parse_line("an error occurred but this has no level token").severity, "INFO")
        self.assertEqual(lp.parse_line("Traceback (most recent call last):").severity, "ERROR")

    def test_lambda_report_line(self):
        r = lp.parse_line("REPORT RequestId: a-b Duration: 120.50 ms Billed Duration: 121 ms Memory Size: 256 MB Max Memory Used: 88 MB Init Duration: 250.10 ms")
        self.assertEqual((r.lambda_duration_ms, r.lambda_memory_mb, r.lambda_init_ms), (120.5, 88.0, 250.1))
        self.assertEqual(r.severity, "INFO")

    def test_timeouts_and_oom_are_critical(self):
        t = lp.parse_line("2026-01-01T00:00:00Z abc Task timed out after 3.00 seconds")
        self.assertEqual((t.severity, t.critical_reason), ("CRITICAL", "timeout after 3.00s"))
        self.assertEqual(lp.parse_line("Runtime exited with error: signal: killed").critical_reason, "out of memory")
        self.assertEqual(lp.parse_line('{"level":"fatal","msg":"x"}').critical_reason, "critical severity")

    def test_garbage_never_raises(self):
        for line in ("", None, "{broken json", "[]", "{}", "\x00\x01", "{" * 5000):
            lp.parse_line(line)
        self.assertTrue(lp.parse_line("{broken json").parse_failed)

    def test_own_output_is_recognised(self):
        self.assertTrue(lp.parse_line('{"_aws":{"Timestamp":1},"X":1}').own_output)
        self.assertTrue(lp.parse_line('{"type":"error_signature","service":"x"}').own_output)

    def test_fingerprint_groups_the_same_error(self):
        a = lp.fingerprint("Timeout for order 4812 at 10.0.0.1 id 3f2b9c1e-1111-2222-3333-444455556666")
        b = lp.fingerprint("timeout for order 99 at 10.9.9.9 id aaaaaaaa-1111-2222-3333-444455556666")
        self.assertEqual(a, b)
        self.assertNotEqual(a, lp.fingerprint("connection refused"))


class AggregateTests(unittest.TestCase):
    def test_buckets_by_minute(self):
        items = [(60_000 * 5 + 10, lp.parse_line('{"level":"error","msg":"a"}')),
                 (60_000 * 5 + 59_999, lp.parse_line('{"level":"warn","msg":"b"}')),
                 (60_000 * 6, lp.parse_line('{"level":"info","msg":"c","latency_ms":40}'))]
        b = aggregate_by_minute(items)
        self.assertEqual(sorted(b), [300_000, 360_000])
        self.assertEqual((b[300_000].errors, b[300_000].warns, b[360_000].latencies), (1, 1, [40.0]))

    def test_signatures_rank_by_frequency_and_ignore_info(self):
        recs = [lp.parse_line('{"level":"error","msg":"db timeout order %d"}' % i) for i in range(5)]
        recs += [lp.parse_line('{"level":"error","msg":"cache miss storm"}'), lp.parse_line('{"level":"info","msg":"ok"}')]
        sigs = error_signatures(recs)
        self.assertEqual([s.count for s in sigs], [5, 1])
        self.assertIn("db timeout", sigs[0].sample)


if __name__ == "__main__":
    unittest.main()
