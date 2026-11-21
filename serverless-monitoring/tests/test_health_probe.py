import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.helpers import Capture, T0_MS
from handlers import health_probe as hp


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/slow":
            time.sleep(0.4)
        code = 500 if self.path == "/fail" else 200
        try:
            self.send_response(code)
            self.end_headers()
            self.wfile.write(b"ok")
        except (BrokenPipeError, ConnectionResetError):
            pass  # the probe gave up (timeout test) before we answered

    def log_message(self, *a):
        pass


class ProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def run_probe(self, targets):
        cap = Capture()
        results = hp.process(targets, emit=lambda recs: cap.emitted.extend(recs), now_ms=T0_MS)
        return cap, {r["name"]: r for r in results}

    def test_healthy_endpoint(self):
        cap, res = self.run_probe([{"name": "ok", "url": self.base + "/ok"}])
        self.assertTrue(res["ok"]["healthy"])
        self.assertEqual(cap.metric("Availability", Target="ok"), [(T0_MS, 1)])
        self.assertEqual(cap.metric("StatusCode", Target="ok")[0][1], 200)
        self.assertGreater(cap.metric("ResponseTimeMs", Target="ok")[0][1], 0)

    def test_error_status_is_down_not_an_exception(self):
        cap, res = self.run_probe([{"name": "bad", "url": self.base + "/fail"}])
        self.assertFalse(res["bad"]["healthy"])
        self.assertEqual(cap.metric("Availability")[0][1], 0)
        self.assertEqual(cap.metric("StatusCode")[0][1], 500)
        self.assertIn("unexpected status 500", cap.emitted[0]["probe_error"])

    def test_expected_status_can_be_configured(self):
        _, res = self.run_probe([{"name": "e", "url": self.base + "/fail", "expect_status": 500}])
        self.assertTrue(res["e"]["healthy"])

    def test_too_slow_counts_as_unavailable(self):
        _, res = self.run_probe([{"name": "slow", "url": self.base + "/slow", "max_latency_ms": 100}])
        self.assertFalse(res["slow"]["healthy"])
        self.assertIn("too slow", res["slow"]["error"])

    def test_timeout_and_connection_refused(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); dead_port = s.getsockname()[1]; s.close()
        cap, res = self.run_probe([{"name": "dead", "url": f"http://127.0.0.1:{dead_port}/"},
                                   {"name": "timeout", "url": self.base + "/slow", "timeout_s": 0.1}])
        self.assertFalse(res["dead"]["healthy"])
        self.assertFalse(res["timeout"]["healthy"])
        self.assertIsNone(res["dead"]["status"])
        self.assertEqual(sorted(v for _, v in cap.metric("Availability")), [0, 0])
        self.assertEqual(cap.metric("StatusCode"), [])  # no status code to report when nothing answered

    def test_targets_are_probed_in_parallel(self):
        targets = [{"name": f"s{i}", "url": self.base + "/slow"} for i in range(5)]
        start = time.time()
        self.run_probe(targets)
        self.assertLess(time.time() - start, 1.2)  # 5 x 0.4 s sequentially would be 2 s

    def test_https_targets_report_certificate_days(self):
        cap = Capture()
        hp.process([{"name": "tls", "url": "https://example.test/health"}], emit=lambda r: cap.emitted.extend(r),
                   fetch=lambda url, t: (200, 12.0, None), cert_check=lambda h, p, t: 42.0, now_ms=T0_MS)
        self.assertEqual(cap.metric("CertDaysRemaining", Target="tls")[0][1], 42.0)

    def test_certificate_check_can_be_disabled(self):
        cap = Capture()
        hp.process([{"name": "tls", "url": "https://example.test/", "check_certificate": False}], emit=lambda r: cap.emitted.extend(r),
                   fetch=lambda url, t: (200, 1.0, None), cert_check=lambda *a: 1 / 0, now_ms=T0_MS)
        self.assertEqual(cap.metric("CertDaysRemaining"), [])

    def test_days_until_expiry(self):
        not_after = "Jan  1 00:00:00 2030 GMT"
        import ssl
        base = ssl.cert_time_to_seconds(not_after)
        self.assertAlmostEqual(hp.days_until(not_after, now=base - 10 * 86400), 10.0)
        self.assertLess(hp.days_until(not_after, now=base + 86400), 0)

    def test_target_validation(self):
        for bad in ([{"name": "x"}], [{"url": "http://a"}], [{"name": "x", "url": "ftp://a"}],
                    [{"name": "x", "url": "http://a"}, {"name": "x", "url": "http://b"}]):
            with self.assertRaises(ValueError):
                hp.validate_targets(bad)

    def test_lambda_handler_reports_down_targets(self):
        import os
        os.environ["PROBE_TARGETS"] = '[{"name":"bad","url":"%s/fail"}]' % self.base
        try:
            import io, contextlib
            with contextlib.redirect_stdout(io.StringIO()) as out:
                result = hp.handler({}, None)
            self.assertEqual(result, {"probed": 1, "down": ["bad"]})
            self.assertIn('"Availability":0', out.getvalue())
        finally:
            del os.environ["PROBE_TARGETS"]


if __name__ == "__main__":
    unittest.main()
