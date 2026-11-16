"""Local checks only. API responses are simulated; no Metronome requests are made."""
from collections import Counter
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.utils import format_datetime
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import raindrop as app

# A fixed test date keeps these historical sample events valid in the tests.
# This does not change the real date used by the script in live mode.
NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)


class BillingTests(unittest.TestCase):
    def setUp(self):
        """Give each test a fresh configuration and keep batch logs out of test output."""
        self.cfg = json.loads((ROOT / "config.example.json").read_text())
        self.events = app.generate(self.cfg)
        self.output = io.StringIO()
        self.quiet = redirect_stdout(self.output)
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)

    def send(self, transport, **kwargs):
        """Use a fake API response provider and replace real waiting with a mock."""
        sleep = kwargs.pop("sleep", Mock())
        app.send_events(self.events, "test-token", transport=transport,
                        sleep=sleep, now=NOW, **kwargs)
        return sleep

    def test_repeatable_360_events_with_correct_customer_provider_and_units(self):
        """Check repeatability, unique IDs, customer/provider counts and property types."""
        self.assertEqual(self.events, app.generate(self.cfg))
        self.assertEqual(len(self.events), 360)
        self.assertEqual(len({e["transaction_id"] for e in self.events}), 360)
        counts = Counter((e["customer_id"], e["properties"]["cloud_provider"]) for e in self.events)
        self.assertEqual(len(counts), 6)
        self.assertEqual(set(counts.values()), {60})
        for event in self.events:
            self.assertEqual(event["timestamp"], event["properties"]["interval_start"])
            self.assertTrue(all(isinstance(v, str) for v in event["properties"].values()))

    def test_expected_totals_and_fractional_hour_measurements(self):
        """Compare expected totals with event sums and preserve 20-minute measurements."""
        rows = app.expected_totals(self.cfg)
        self.assertEqual([r["expected_hours"] for r in rows], ["15", "15", "30", "30", "60", "60"])
        self.assertEqual(sum(Decimal(r["expected_usd"]) for r in rows[:2]), Decimal("15.45"))
        for row in rows:
            seconds = sum(int(e["properties"]["compute_seconds"]) for e in self.events
                          if (e["customer_id"], e["properties"]["cloud_provider"])
                          == (row["customer"], row["provider"]))
            self.assertEqual(seconds, row["expected_seconds"])
        # Twenty minutes is one third of an hour. Rounding each event to 0.33
        # hours would give 19.8 hours; summing seconds first correctly gives 20.
        self.cfg["customers"][0]["seconds_per_window"] = 1200
        self.assertEqual(app.generate(self.cfg)[0]["properties"]["compute_seconds"], "1200")
        self.assertEqual(app.expected_totals(self.cfg)[0]["expected_hours"], "20")

    def test_invalid_configuration_and_billing_boundaries(self):
        """Reject bad durations, missing metadata, identity collisions and invalid dates."""
        invalid = []
        for value in (0, -1, 3601, 0.25, True):
            # Change a copy so each example tests its own invalid input.
            cfg = deepcopy(self.cfg)
            cfg["customers"][0]["seconds_per_window"] = value
            invalid.append(cfg)
        for change in ("region", "alias", "warehouse", "period", "timezone"):
            cfg = deepcopy(self.cfg)
            if change == "region":
                cfg["customers"][0]["warehouses"]["aws"]["region"] = ""
            elif change == "alias":
                cfg["customers"][1]["alias"] = cfg["customers"][0]["alias"]
            elif change == "warehouse":
                cfg["customers"][1]["warehouses"]["aws"]["id"] = "wh_dm_aws_01"
            elif change == "period":
                cfg["start"] = "2026-11-30T00:00:00Z"
            else:
                cfg["start"] = "2026-11-08T00:00:00"
            invalid.append(cfg)
        for cfg in invalid:
            with self.subTest(config=cfg), self.assertRaises(ValueError):
                app.generate(cfg)

    def test_default_dry_run_writes_data_without_sending(self):
        """Run the default command in a temporary folder and prove the sender is unused."""
        with tempfile.TemporaryDirectory() as folder:
            args = ["raindrop.py", "--config", str(ROOT / "config.example.json"), "--out-dir", folder]
            with patch.object(sys, "argv", args), patch.object(app, "send_events") as send:
                app.main()
                send.assert_not_called()
            self.assertEqual(json.loads((Path(folder) / "events.json").read_text()), self.events)
        self.assertIn("360 events; 4 batches; no network requests", self.output.getvalue())

    def test_batches_and_reruns_preserve_all_ids(self):
        """A rerun sends the same four batches again; there is no local skip ledger."""
        transport = Mock(return_value=(200, {}))
        self.send(transport)
        self.send(transport)
        batches = [call.args[0] for call in transport.call_args_list]
        self.assertEqual([len(b) for b in batches], [100, 100, 100, 60] * 2)
        self.assertEqual(batches[:4], batches[4:])

    def test_timeout_retries_the_identical_batch(self):
        """Simulate a lost response, then success, and compare the two requests."""
        transport = Mock(side_effect=[(0, {}), (200, {}), (200, {}), (200, {}), (200, {})])
        sleep = self.send(transport)
        self.assertEqual(transport.call_args_list[0].args, transport.call_args_list[1].args)
        sleep.assert_called_once()

    def test_rate_limit_and_server_error_retry_with_backoff(self):
        """Simulate throttling and a server outage before the same batch succeeds."""
        transport = Mock(side_effect=[(429, {"Retry-After": "3"}), (503, {}),
                                      (200, {}), (200, {}), (200, {}), (200, {})])
        sleep = self.send(transport)
        self.assertEqual(sleep.call_count, 2)
        self.assertGreaterEqual(sleep.call_args_list[0].args[0], 3)
        self.assertEqual(transport.call_args_list[0].args, transport.call_args_list[2].args)

    def test_permanent_errors_stop_without_retry_or_next_batch(self):
        """Bad requests, access errors and redirects must stop further delivery."""
        for status in (400, 401, 403, 302):
            transport, sleep = Mock(return_value=(status, {})), Mock()
            with self.subTest(status=status), self.assertRaises(RuntimeError):
                self.send(transport, sleep=sleep)
            transport.assert_called_once()
            sleep.assert_not_called()

    def test_retry_exhaustion_stops_after_five_attempts(self):
        """A continuing failure gets five requests and four waits, then stops."""
        transport, sleep = Mock(return_value=(503, {})), Mock()
        with self.assertRaisesRegex(RuntimeError, "Retry limit"):
            self.send(transport, sleep=sleep)
        self.assertEqual(transport.call_count, 5)
        self.assertEqual(sleep.call_count, 4)

    def test_live_send_blocks_old_or_unfinished_windows(self):
        """Block invalid live dates before the first API request is attempted."""
        for now in (datetime(2027, 1, 1, tzinfo=timezone.utc), datetime(2026, 11, 8, tzinfo=timezone.utc)):
            transport = Mock()
            with self.subTest(now=now), self.assertRaises(ValueError):
                app.send_events(self.events, "test-token", transport=transport, now=now)
            transport.assert_not_called()

    def test_invalid_tokens_are_blocked_without_being_exposed(self):
        """Reject missing or malformed tokens without including the secret in the error."""
        for token in (None, "", "secret\nvalue"):
            transport = Mock()
            with self.subTest(token=bool(token)), self.assertRaises(ValueError) as raised:
                app.send_events(self.events, token, transport=transport, now=NOW)
            self.assertNotIn("secret", str(raised.exception))
            transport.assert_not_called()

    def test_retry_after_seconds_date_and_invalid_header(self):
        """Respect either server delay format and fall back when the header is invalid."""
        self.assertGreaterEqual(app.retry_delay({"retry-after": "10"}, 0), 10)
        later = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
        self.assertGreater(app.retry_delay({"Retry-After": later}, 0), 25)
        self.assertGreaterEqual(app.retry_delay({"Retry-After": "invalid"}, 0), 1)

    def test_http_request_and_uncertain_transport_outcomes(self):
        """Inspect the outgoing JSON and timeout, then simulate network and HTTP errors."""
        # Replace the HTTP opener itself, so this test cannot contact Metronome.
        with patch.object(app, "build_opener") as build:
            response = build.return_value.open.return_value.__enter__.return_value
            response.status, response.headers = 200, {}
            self.assertEqual(app.post_ingest(self.events[:1], "test-token"), (200, {}))
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(json.loads(request.data), self.events[:1])
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(build.return_value.open.call_args.kwargs["timeout"], 30)
            handler = build.call_args.args[0]
            self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, "https://example.com"))
            build.return_value.open.side_effect = TimeoutError()
            self.assertEqual(app.post_ingest(self.events[:1], "test-token"), (0, {}))
            build.return_value.open.side_effect = HTTPError(app.INGEST_URL, 429, "limit", {}, None)
            self.assertEqual(app.post_ingest(self.events[:1], "test-token"), (429, {}))


if __name__ == "__main__":
    unittest.main()
