#!/usr/bin/env python3
"""Generate the assessment's usage events; send only with --send. Standard library only."""
import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import random
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

UTC = timezone.utc
EVENT_TYPE = "warehouse_compute"
HOURS = 60
BATCH_SIZE = 100
MAX_ATTEMPTS = 5
INGEST_URL = "https://api.metronome.com/v1/ingest"
RATES = {"aws": Decimal("0.54"), "gcp": Decimal("0.49")}


def parse_utc(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("Use UTC dates with a Z or +00:00 suffix")
    return parsed


def stamp(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def generate(cfg):
    """Check the configuration, then build one event per customer/provider/hour."""
    start = parse_utc(cfg["start"])
    period_start, period_end = map(parse_utc, (cfg["period_start"], cfg["period_end"]))
    if start.minute or start.second or start.microsecond:
        raise ValueError("Start must be on a UTC hour boundary")
    if not period_start <= start < start + timedelta(hours=HOURS) <= period_end:
        raise ValueError("All hourly windows must fit within the billing period")
    customers = cfg["customers"]
    aliases = [customer["alias"] for customer in customers]
    if len(aliases) != 3 or any(not isinstance(a, str) or not a.strip() for a in aliases):
        raise ValueError("Provide three nonempty customer aliases")
    if len(set(aliases)) != 3:
        raise ValueError("Customer aliases must be distinct")
    warehouse_ids = set()
    for customer in customers:
        seconds = customer["seconds_per_window"]
        if type(seconds) is not int or not 0 < seconds <= 3600:
            raise ValueError("Each hourly window needs integer seconds from 1 to 3600")
        if set(customer["warehouses"]) != set(RATES):
            raise ValueError("Each customer needs an AWS and a GCP warehouse")
        for resource in customer["warehouses"].values():
            if any(not isinstance(resource[k], str) or not resource[k].strip()
                   for k in ("id", "region", "size")):
                raise ValueError("Warehouse ID, region and size must be present")
            if resource["id"] in warehouse_ids or len(resource["id"]) > 64:
                raise ValueError("Use distinct warehouse IDs of at most 64 characters")
            warehouse_ids.add(resource["id"])

    events = []
    for customer in customers:
        for provider in RATES:
            resource = customer["warehouses"][provider]
            warehouse = resource["id"]
            for index in range(HOURS):
                begin = start + timedelta(hours=index)
                end = begin + timedelta(hours=1)
                tid = f"{EVENT_TYPE}:{warehouse}:{stamp(begin)}"
                seconds = customer["seconds_per_window"]
                events.append({
                    "transaction_id": tid, "customer_id": customer["alias"],
                    "timestamp": stamp(begin), "event_type": EVENT_TYPE,
                    "properties": {
                        "cloud_provider": provider, "warehouse_id": warehouse,
                        "warehouse_size": resource["size"], "region": resource["region"],
                        "compute_seconds": str(seconds),
                        "interval_start": stamp(begin), "interval_end": stamp(end),
                        "schema_version": "1",
                    },
                })
    return events


def expected_totals(cfg):
    """Calculate expectations from configured durations, independently of event JSON."""
    rows = []
    for customer in cfg["customers"]:
        seconds = HOURS * customer["seconds_per_window"]
        hours = Decimal(seconds) / Decimal(3600)
        for provider, rate in RATES.items():
            rows.append({
                "customer": customer["alias"], "provider": provider,
                "expected_seconds": seconds, "expected_hours": str(hours),
                "rate_usd_per_hour": str(rate), "expected_usd": str(hours * rate),
            })
    return rows


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # Do not forward an authenticated request to another endpoint.


def post_ingest(batch, token):
    request = Request(INGEST_URL, data=json.dumps(batch).encode(), method="POST",
                      headers={"Authorization": "Bearer " + token,
                               "Content-Type": "application/json"})
    try:
        with build_opener(NoRedirect).open(request, timeout=30) as response:
            return response.status, dict(response.headers)
    except HTTPError as exc:
        return exc.code, dict(exc.headers)
    except (URLError, TimeoutError, OSError):
        return 0, {}  # The result is unknown; retry the unchanged events.


def retry_delay(headers, attempt):
    delay = min(60, 2 ** attempt) + random.random()
    supplied = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    if supplied:
        try:
            delay = max(delay, float(supplied))
        except ValueError:
            try:
                delay = max(delay, (parsedate_to_datetime(supplied) - datetime.now(UTC)).total_seconds())
            except (TypeError, ValueError):
                pass
    return delay


def send_events(events, token, transport=post_ingest, sleep=time.sleep, now=None):
    """Send sequential batches. A rerun sends all events again with their original IDs."""
    if not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ValueError("Load a valid API token locally; do not put it in the config")
    now = now or datetime.now(UTC)
    for event in events:
        if (parse_utc(event["timestamp"]) < now - timedelta(days=34)
                or parse_utc(event["properties"]["interval_end"]) > now):
            raise ValueError("Live sending requires completed windows within 34 days; do not retimestamp old usage")
    for offset in range(0, len(events), BATCH_SIZE):
        batch = events[offset:offset + BATCH_SIZE]
        for attempt in range(MAX_ATTEMPTS):
            status, headers = transport(batch, token)
            print(f"Batch {offset // BATCH_SIZE + 1}: {len(batch)} events; attempt {attempt + 1}; HTTP {status or 'unknown'}")
            if status == 200:
                break
            if status not in (0, 429) and not 500 <= status <= 599:
                raise RuntimeError(f"HTTP {status}: stopped; investigate before sending again")
            if attempt == MAX_ATTEMPTS - 1:
                raise RuntimeError("Retry limit reached; check the outcome before rerunning unchanged data")
            sleep(retry_delay(headers, attempt))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.example.json")
    parser.add_argument("--out-dir", default="scratch")
    parser.add_argument("--send", action="store_true", help="Send to the account selected by the local API token")
    args = parser.parse_args()
    try:
        cfg = json.loads(Path(args.config).read_text())
        events = generate(cfg)
        totals = expected_totals(cfg)
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        for name, value in (("events.json", events), ("expected-totals.json", totals)):
            (out / name).write_text(json.dumps(value, indent=2) + "\n")
        if not args.send:
            print(f"DRY RUN: {len(events)} events; {(len(events) + BATCH_SIZE - 1) // BATCH_SIZE} batches; no network requests")
        else:
            send_events(events, os.environ.get("METRONOME_BEARER_TOKEN"))
            print(f"Accepted {len(events)} events. Verify customer usage and invoice line items separately.")
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
        print(f"Stopped: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
