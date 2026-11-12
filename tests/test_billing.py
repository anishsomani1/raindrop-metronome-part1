"""Local tests; all API outcomes below are mocks, never sandbox evidence."""
import copy
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from raindrop import generate, validate_dataset, ingest, digest, retry_delay, validate_token
from reconcile import expected, compare, USD_CENTS_ID

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)


class BillingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads((ROOT / "config.example.json").read_text())
        self.cfg["metrics_confirmed"] = True
        self.cfg["products_confirmed"] = True
        self.data = generate(self.cfg)
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "state.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def run_ingest(self, transport, **kwargs):
        return ingest(self.data, self.db, "MOCK-NOT-A-REAL-TOKEN", send=True,
                      now=NOW, transport=transport, sleep=lambda _: None, **kwargs)

    def test_360_unique_deterministic_events(self):
        self.assertEqual(self.data, generate(self.cfg))
        self.assertEqual(validate_dataset(self.data), 360)

    def test_independent_totals_and_no_per_event_rounding(self):
        rows = expected(self.data)
        self.assertEqual([r["expected_usage_hours"] for r in rows], ["15", "15", "60", "60", "30", "30"])
        dunder = [r for r in rows if r["customer"] == "rd-dunder-mifflin"]
        self.assertEqual(sum(Decimal(r["expected_charge_usd"]) for r in dunder), Decimal("15.45"))
        corrupted_events = copy.deepcopy(self.data)
        corrupted_events["events"][0]["properties"]["compute_seconds"] = "999"
        self.assertEqual(expected(corrupted_events), rows)  # Calculator uses measurement seconds.
        self.assertIsNone(rows[0]["observed_usage_hours"])

    def test_incorrect_attribution_and_malformed_data_rejected(self):
        for key, value in [("cloud_provider", "azure"), ("compute_seconds", "NaN"),
                           ("compute_seconds", 900), ("compute_seconds", "0.25"),
                           ("compute_seconds", "0"), ("compute_seconds", "3601")]:
            d = copy.deepcopy(self.data)
            d["events"][0]["properties"][key] = value
            d["events_sha256"] = digest(d["events"])
            with self.assertRaises(ValueError):
                validate_dataset(d)

    def test_period_boundaries(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["start"] = "2026-11-28T12:00:00Z"  # Ends exactly at exclusive period end.
        validate_dataset(generate(cfg))
        cfg["start"] = "2026-11-28T13:00:00Z"
        with self.assertRaises(ValueError):
            generate(cfg)

    def test_stale_and_unfinished_usage_blocked(self):
        for now in [datetime(2026, 12, 20, tzinfo=timezone.utc), datetime(2026, 11, 9, tzinfo=timezone.utc)]:
            with self.assertRaises(ValueError):
                validate_dataset(self.data, now=now, ingestion=True)

    def test_dry_run_never_calls_transport(self):
        result = ingest(self.data, self.db, transport=lambda *_: self.fail("network called"))
        self.assertEqual(result["mode"], "dry-run")
        self.assertFalse(Path(self.db).exists())

    def test_metric_order_gate(self):
        self.cfg["metrics_confirmed"] = False
        self.data = generate(self.cfg)
        with self.assertRaises(ValueError):
            self.run_ingest(lambda *_: self.fail("network called"))

    def test_product_conversion_gate(self):
        self.cfg["products_confirmed"] = False
        self.data = generate(self.cfg)
        with self.assertRaisesRegex(ValueError, "divide-by-3600"):
            self.run_ingest(lambda *_: self.fail("network called"))

    def test_whole_second_measurements_and_twenty_minute_runs(self):
        for invalid in (True, 900.0, "900", 0, -1, 3601):
            cfg = copy.deepcopy(self.cfg)
            cfg["customers"][0]["seconds_per_window"] = invalid
            with self.assertRaises(ValueError):
                generate(cfg)
        self.cfg["customers"][0]["seconds_per_window"] = 1200
        data = generate(self.cfg)
        self.assertEqual(data["events"][0]["properties"]["compute_seconds"], "1200")
        self.assertEqual(data["measurements"][0]["active_seconds"], 1200)
        dunder = [r for r in expected(data) if r["customer"] == "rd-dunder-mifflin"]
        self.assertEqual([r["expected_usage_hours"] for r in dunder], ["20", "20"])
        self.assertEqual([r["expected_charge_usd"] for r in dunder], ["10.80", "9.80"])

    def test_warehouse_identity_and_metadata(self):
        first = self.data["events"][0]
        self.assertEqual(first["transaction_id"], "warehouse_compute:wh_dm_aws_01:2026-11-08T00:00:00Z")
        self.assertEqual(first["event_type"], "warehouse_compute")
        self.assertEqual(first["properties"]["region"], "us-east-1")
        self.assertEqual(self.data["events"][60]["properties"]["region"], "us-central1")
        self.assertEqual(set(first["properties"]), {"cloud_provider", "compute_seconds", "warehouse_id",
            "warehouse_size", "region", "interval_start", "interval_end", "schema_version"})
        self.cfg["customers"][1]["warehouses"]["aws"]["id"] = "wh_dm_aws_01"
        with self.assertRaisesRegex(ValueError, "globally unique"):
            generate(self.cfg)

    def test_batch_limits_and_restart_skip(self):
        batches = []
        self.run_ingest(lambda b, _: (batches.append(len(b)) or 200, {}))
        self.assertEqual(batches, [100, 100, 100, 60])
        report = self.run_ingest(lambda *_: self.fail("accepted events resent"))
        self.assertEqual(report["skipped_previously_accepted"], 360)

    def test_duplicate_replay_mock(self):
        accepted_ids = set()
        calls = []
        def mock(b, _):
            calls.append([e["transaction_id"] for e in b])
            accepted_ids.update(calls[-1])
            return 200, {}
        self.run_ingest(mock)
        self.run_ingest(mock, replay=True)
        self.assertEqual(len(accepted_ids), 360)
        self.assertEqual(calls[:4], calls[4:])

    def test_partial_ambiguous_failure_retries_unchanged_mock(self):
        batches, server_ids = [], set()
        def mock(batch, _):
            batches.append(copy.deepcopy(batch))
            if len(batches) == 1:
                server_ids.update(e["transaction_id"] for e in batch[:23])
                return 0, {}  # Lost response after partial acceptance.
            server_ids.update(e["transaction_id"] for e in batch)
            return 200, {}
        self.run_ingest(mock)
        self.assertEqual(batches[0], batches[1])
        self.assertEqual(len(server_ids), 360)

    def test_retryable_429_and_503_mock(self):
        outcomes = iter([429, 503, 200, 200, 200, 200])
        self.assertEqual(self.run_ingest(lambda *_: (next(outcomes), {}))["accepted_this_run"], 360)
        self.assertGreaterEqual(retry_delay({"Retry-After": "20"}, 0, lambda: 0), 20)

    def test_permanent_400_stops_and_stays_held_mock(self):
        with self.assertRaises(RuntimeError):
            self.run_ingest(lambda *_: (400, {}))
        with self.assertRaises(ValueError):
            self.run_ingest(lambda *_: self.fail("permanent failure retried"))

    def test_exhausted_retries_preserve_pending_mock(self):
        with self.assertRaises(RuntimeError):
            self.run_ingest(lambda *_: (503, {}), max_attempts=2)
        self.assertEqual(self.run_ingest(lambda *_: (200, {}))["accepted_this_run"], 360)

    def test_changed_measurement_conflicts(self):
        self.run_ingest(lambda *_: (200, {}))
        old_ids = [e["transaction_id"] for e in self.data["events"]]
        self.cfg["customers"][0]["seconds_per_window"] = 1800
        self.data = generate(self.cfg)
        self.assertEqual(old_ids, [e["transaction_id"] for e in self.data["events"]])
        with self.assertRaises(ValueError):
            self.run_ingest(lambda *_: self.fail("conflict sent"))

    def test_renaming_dataset_does_not_create_new_usage(self):
        self.run_ingest(lambda *_: (200, {}))
        self.cfg["dataset_id"] = "renamed-dataset"
        original_events = self.data["events"]
        self.data = generate(self.cfg)
        self.assertEqual(original_events, self.data["events"])
        with self.assertRaises(ValueError):
            self.run_ingest(lambda *_: self.fail("overlap sent"))

    def test_synthetic_invoice_reconciliation(self):
        mapping = {"customer_id": "SYNTHETIC-CUSTOMER", "customer_alias": "rd-dunder-mifflin",
            "contract_id": "SYNTHETIC-CONTRACT", "products": {"P-AWS": "aws", "P-GCP": "gcp"},
            "start_timestamp": self.cfg["period_start"], "end_timestamp": self.cfg["period_end"]}
        inv = {"data": {"id": "SYNTHETIC-INVOICE", "type": "USAGE", "status": "DRAFT",
            "customer_id": mapping["customer_id"], "contract_id": mapping["contract_id"],
            "start_timestamp": mapping["start_timestamp"], "end_timestamp": mapping["end_timestamp"],
            "credit_type": {"id": USD_CENTS_ID}, "total": 1545,
            "line_items": [{"product_id": p, "product_type": "UsageProductListItem",
                "credit_type": {"id": USD_CENTS_ID}, "quantity": 15, "unit_price": price, "total": 15 * price}
                for p, price in [("P-AWS", 54), ("P-GCP", 49)]]}}
        rows = compare(self.data, inv, mapping)
        self.assertEqual(rows[0]["difference_usd"], "0.00")
        bad_alias = {**mapping, "customer_alias": "rd-typo"}
        with self.assertRaises(ValueError):
            compare(self.data, inv, bad_alias)
        empty = {**self.data, "measurements": []}
        with self.assertRaises(ValueError):
            compare(empty, inv, mapping)
        old_mapping = {**mapping, "start_timestamp": "2026-10-01T00:00:00Z", "end_timestamp": "2026-11-01T00:00:00Z"}
        old_invoice = copy.deepcopy(inv)
        old_invoice["data"].update({k: old_mapping[k] for k in ("start_timestamp", "end_timestamp")})
        with self.assertRaises(ValueError):
            compare(self.data, old_invoice, old_mapping)
        inv["data"]["line_items"][0]["commit_id"] = "COMMIT"
        with self.assertRaises(ValueError):
            compare(self.data, inv, mapping)

    def test_all_customer_usage_observations_checked_without_invoice(self):
        obs = {"rd-initech": {"aws": {"hours": "30", "source_file": "MOCK-usage.json",
            "query_start": "2026-11-08T00:00:00Z", "query_end": "2026-11-11T00:00:00Z"}}}
        rows = compare(self.data, observations=obs)
        self.assertEqual(next(r for r in rows if r["customer"] == "rd-initech" and r["provider"] == "aws")["observed_usage_hours"], "30")
        obs["rd-initech"]["aws"]["hours"] = "999"
        with self.assertRaises(ValueError):
            compare(self.data, observations=obs)

    def test_malformed_token_cannot_leak_into_exception(self):
        token = "MOCK-SECRET\nSECOND-LINE"
        with self.assertRaises(ValueError) as caught:
            validate_token(token)
        self.assertNotIn("MOCK-SECRET", str(caught.exception))
        with self.assertRaises(ValueError):
            ingest(self.data, self.db, token, send=True, now=NOW, transport=lambda *_: self.fail("bad token sent"))

    def test_seconds_observations_are_converted_once(self):
        obs = {"rd-dunder-mifflin": {"aws": {"seconds": "54000", "source_file": "MOCK-seconds-usage.json",
            "query_start": "2026-11-08T00:00:00Z", "query_end": "2026-11-11T00:00:00Z"}}}
        rows = compare(self.data, observations=obs)
        row = next(r for r in rows if r["customer"] == "rd-dunder-mifflin" and r["provider"] == "aws")
        self.assertEqual(row["observed_usage_seconds"], "54000")
        self.assertEqual(row["observed_usage_hours"], "15")
        obs["rd-dunder-mifflin"]["aws"]["hours"] = "15"
        with self.assertRaisesRegex(ValueError, "exactly one explicit unit"):
            compare(self.data, observations=obs)
        del obs["rd-dunder-mifflin"]["aws"]["hours"]
        obs["rd-dunder-mifflin"]["aws"]["seconds"] = "15"
        with self.assertRaisesRegex(ValueError, "Observed usage differs"):
            compare(self.data, observations=obs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
