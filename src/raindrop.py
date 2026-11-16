#!/usr/bin/env python3
"""Generate sample warehouse usage for the Raindrop assessment.

Start reading at main(): load config -> generate events -> calculate expectations
-> save the output -> optionally send to Metronome. Dry run is the default.
The configuration supplies sample running times; this does not monitor real warehouses.
Everything uses Python's standard library, so no packages need installing.
"""
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

# The assessment uses 60 hourly windows for each customer and cloud provider.
UTC = timezone.utc
EVENT_TYPE = "warehouse_compute"
HOURS = 60
BATCH_SIZE = 100
MAX_ATTEMPTS = 5
INGEST_URL = "https://api.metronome.com/v1/ingest"
# These prices calculate local expectations. Metronome applies its own rate card;
# prices are never included in the usage events. Decimal avoids float rounding errors.
RATES = {"aws": Decimal("0.54"), "gcp": Decimal("0.49")}


def parse_utc(value):
    """Read a date from the config and require an explicit UTC timezone."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("Use UTC dates with a Z or +00:00 suffix")
    return parsed


def stamp(value):
    """Format a validated UTC date for an event; Z means UTC."""
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def generate(cfg):
    """Check the configuration, then build one event per customer/provider/hour."""
    # Keep all measurements within the chosen billing period, starting on an hour.
    start = parse_utc(cfg["start"])
    period_start, period_end = map(parse_utc, (cfg["period_start"], cfg["period_end"]))
    if start.minute or start.second or start.microsecond:
        raise ValueError("Start must be on a UTC hour boundary")
    if not period_start <= start < start + timedelta(hours=HOURS) <= period_end:
        raise ValueError("All hourly windows must fit within the billing period")
    # Aliases identify the three customers. Their existence in Metronome is a
    # setup prerequisite, not something this local check verifies through the API.
    customers = cfg["customers"]
    aliases = [customer["alias"] for customer in customers]
    if len(aliases) != 3 or any(not isinstance(a, str) or not a.strip() for a in aliases):
        raise ValueError("Provide three nonempty customer aliases")
    if len(set(aliases)) != 3:
        raise ValueError("Customer aliases must be distinct")
    warehouse_ids = set()
    for customer in customers:
        seconds = customer["seconds_per_window"]
        # Running time cannot exceed this one-hour window. The strict type check
        # also excludes True/False, which Python otherwise treats as integers.
        if type(seconds) is not int or not 0 < seconds <= 3600:
            raise ValueError("Each hourly window needs integer seconds from 1 to 3600")
        if set(customer["warehouses"]) != set(RATES):
            raise ValueError("Each customer needs an AWS and a GCP warehouse")
        # The configured metrics require this metadata. Unique warehouse IDs
        # also prevent different resources from sharing the same transaction ID.
        for resource in customer["warehouses"].values():
            if any(not isinstance(resource[k], str) or not resource[k].strip()
                   for k in ("id", "region", "size")):
                raise ValueError("Warehouse ID, region and size must be present")
            if resource["id"] in warehouse_ids or len(resource["id"]) > 64:
                raise ValueError("Use distinct warehouse IDs of at most 64 characters")
            warehouse_ids.add(resource["id"])

    # Three customers x two providers x 60 windows produces 360 events.
    events = []
    for customer in customers:
        for provider in RATES:
            resource = customer["warehouses"][provider]
            warehouse = resource["id"]
            for index in range(HOURS):
                # Move forward one hour at a time; actual running seconds come
                # from the config and may be less than the full reporting window.
                begin = start + timedelta(hours=index)
                end = begin + timedelta(hours=1)
                # Identify the measurement, not the send attempt. Regenerating
                # unchanged usage keeps this ID; retries must not invent new IDs.
                tid = f"{EVENT_TYPE}:{warehouse}:{stamp(begin)}"
                seconds = customer["seconds_per_window"]
                # customer_id attributes usage; timestamp places it in a period.
                # Provider selects the metric, which sums compute_seconds.
                # Properties are strings; schema_version labels our event format.
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
        # Example: 60 windows x 900 seconds / 3,600 = 15 hours per provider.
        # This predicts the bill; it does not read or verify a Metronome invoice.
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
    """Stop an unexpected redirect from forwarding the authenticated request."""
    def redirect_request(self, *args, **kwargs):
        return None  # Do not forward an authenticated request to another endpoint.


def post_ingest(batch, token):
    """Send one batch and return its HTTP status and response headers."""
    # JSON is sent as bytes. The token goes in the authorization header, not the body.
    request = Request(INGEST_URL, data=json.dumps(batch).encode(), method="POST",
                      headers={"Authorization": "Bearer " + token,
                               "Content-Type": "application/json"})
    try:
        with build_opener(NoRedirect).open(request, timeout=30) as response:
            return response.status, dict(response.headers)
    except HTTPError as exc:
        # Return the error status so send_events() decides whether to retry.
        return exc.code, dict(exc.headers)
    except (URLError, TimeoutError, OSError):
        return 0, {}  # The result is unknown; retry the unchanged events.


def retry_delay(headers, attempt):
    """Choose a retry delay, respecting a longer wait requested by the server."""
    # Exponential backoff waits longer after repeated failures. A little random
    # delay (jitter) helps keep separate clients from retrying at the same instant.
    delay = min(60, 2 ** attempt) + random.random()
    supplied = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    if supplied:
        # Retry-After can specify either seconds to wait or an HTTP date.
        try:
            delay = max(delay, float(supplied))
        except ValueError:
            try:
                delay = max(delay, (parsedate_to_datetime(supplied) - datetime.now(UTC)).total_seconds())
            except (TypeError, ValueError):
                # An unreadable header leaves the normal backoff delay in place.
                pass
    return delay


def send_events(events, token, transport=post_ingest, sleep=time.sleep, now=None):
    """Send sequential batches; reruns resend all events with their original IDs.

    Tests replace transport, sleep and now to simulate responses without real
    requests or waiting. Live use defaults to the HTTP sender and current time.
    """
    # This checks the token's local format, not its permissions or account.
    if not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ValueError("Load a valid API token locally; do not put it in the config")
    now = now or datetime.now(UTC)
    # Live-only date checks: windows must be finished and recent enough to ingest.
    # This is event age, not a record of when Metronome first accepted an ID.
    for event in events:
        if (parse_utc(event["timestamp"]) < now - timedelta(days=34)
                or parse_utc(event["properties"]["interval_end"]) > now):
            raise ValueError("Live sending requires completed windows within 34 days; do not retimestamp old usage")
    # Take up to 100 events at a time: the last batch contains the remaining 60.
    for offset in range(0, len(events), BATCH_SIZE):
        batch = events[offset:offset + BATCH_SIZE]
        # Keep this batch unchanged across retries. Metronome recognises repeated
        # transaction IDs within its duplicate-detection window after acceptance.
        for attempt in range(MAX_ATTEMPTS):
            status, headers = transport(batch, token)
            print(f"Batch {offset // BATCH_SIZE + 1}: {len(batch)} events; attempt {attempt + 1}; HTTP {status or 'unknown'}")
            if status == 200:
                # Accepted by the API; customer usage and billing still need checking.
                break
            # 0 means no reliable response, 429 means rate-limited, and 5xx means
            # a server error. Other statuses need investigation, not automatic retries.
            if status not in (0, 429) and not 500 <= status <= 599:
                raise RuntimeError(f"HTTP {status}: stopped; investigate before sending again")
            # Five attempts includes the first request; stop if none succeeded.
            if attempt == MAX_ATTEMPTS - 1:
                raise RuntimeError("Retry limit reached; check the outcome before rerunning unchanged data")
            sleep(retry_delay(headers, attempt))


def main():
    """Read command-line options and run the workflow in order."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.example.json")
    parser.add_argument("--out-dir", default="scratch")
    parser.add_argument("--send", action="store_true", help="Send to the account selected by the local API token")
    args = parser.parse_args()
    try:
        # Generate once, then save the same data that live mode would send.
        cfg = json.loads(Path(args.config).read_text())
        events = generate(cfg)
        totals = expected_totals(cfg)
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        for name, value in (("events.json", events), ("expected-totals.json", totals)):
            (out / name).write_text(json.dumps(value, indent=2) + "\n")
        # No --send means no API requests and no token is required.
        if not args.send:
            print(f"DRY RUN: {len(events)} events; {(len(events) + BATCH_SIZE - 1) // BATCH_SIZE} batches; no network requests")
        else:
            # The environment variable keeps credentials out of the config and code.
            send_events(events, os.environ.get("METRONOME_BEARER_TOKEN"))
            print(f"Accepted {len(events)} events. Verify customer usage and invoice line items separately.")
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
        # Exit with a failure code so an incomplete run cannot appear successful.
        print(f"Stopped: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    # Run the command when launched directly, but not when imported by the tests.
    main()
