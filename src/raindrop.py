#!/usr/bin/env python3
"""Raindrop assessment: deterministic usage and controlled Metronome ingestion.

Python 3.11+, standard library only. No SDK and no QBO connector.
All network mutations require the explicit `--send` option and a local token.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import random
import re
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler

UTC = timezone.utc
EVENT_TYPE = "warehouse_compute"
DATASET_FORMAT_VERSION = 2
BASE_URL = "https://api.metronome.com"
LOG = logging.getLogger("raindrop")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def parse_time(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        raise ValueError("Use a UTC timestamp with seconds, e.g. 2026-11-08T00:00:00Z")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def stamp(value):
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def validate_config(cfg):
    if cfg.get("environment") != "assessment-sandbox":
        raise ValueError("Configuration must identify the assessment sandbox")
    if not re.fullmatch(r"[a-z0-9-]{1,40}", cfg["dataset_id"]):
        raise ValueError("dataset_id must be 1-40 lowercase letters, digits or hyphens")
    customers = cfg["customers"]
    if len(customers) != 3 or len({x["alias"] for x in customers}) != 3:
        raise ValueError("Exactly three distinct customer aliases are required")
    warehouse_ids = set()
    for c in customers:
        if not re.fullmatch(r"[a-z0-9-]{1,80}", c["alias"]):
            raise ValueError("Use non-personal, lowercase ingest aliases")
        seconds = c["seconds_per_window"]
        if type(seconds) is not int or not 0 < seconds <= 3600:
            raise ValueError("Each hourly window needs integer seconds from 1 to 3600")
        if set(c["warehouses"]) != {"aws", "gcp"}:
            raise ValueError("Each customer needs one AWS and one GCP warehouse")
        for warehouse in c["warehouses"].values():
            wid = warehouse["id"]
            if not isinstance(wid, str) or not re.fullmatch(r"[a-z0-9_-]{1,64}", wid) or wid in warehouse_ids:
                raise ValueError("Warehouse IDs must be globally unique, stable identifiers of 1-64 characters")
            warehouse_ids.add(wid)
            for field in ("region", "size"):
                if not isinstance(warehouse[field], str) or not re.fullmatch(r"[a-z0-9-]{1,64}", warehouse[field]):
                    raise ValueError("Warehouse region and size must be nonempty lowercase identifiers")
    start = parse_time(cfg["start"])
    end = start + timedelta(hours=60)
    if start.minute or start.second:
        raise ValueError("The fixture starts on a UTC hour boundary")
    period_start, period_end = map(parse_time, (cfg["period_start"], cfg["period_end"]))
    if not period_start <= start < end <= period_end:
        raise ValueError("All 60 hourly windows must fit inside the selected billing period")


def generate(cfg):
    validate_config(cfg)
    start = parse_time(cfg["start"])
    events, measurements = [], []
    for customer in cfg["customers"]:
        for provider in ("aws", "gcp"):
            resource = customer["warehouses"][provider]
            warehouse = resource["id"]
            for index in range(60):
                begin = start + timedelta(hours=index)
                end = begin + timedelta(hours=1)
                # Identity describes a measurement, not its amount. A changed amount under
                # the same identity is a conflict, not a new billable event.
                # This fixture has globally unique warehouse IDs and exactly one
                # completed measurement per warehouse per UTC hour. Dataset labels,
                # schema versions and retry attempts never enter event identity.
                tid = f"{EVENT_TYPE}:{warehouse}:{stamp(begin)}"
                seconds = customer["seconds_per_window"]
                measurements.append({"transaction_id": tid, "customer_alias": customer["alias"],
                    "cloud_provider": provider, "warehouse_id": warehouse,
                    "interval_start": stamp(begin), "interval_end": stamp(end),
                    "active_seconds": seconds})
                events.append({"transaction_id": tid, "customer_id": customer["alias"],
                    "timestamp": stamp(begin), "event_type": EVENT_TYPE,
                    "properties": {"cloud_provider": provider, "warehouse_id": warehouse,
                        "warehouse_size": resource["size"], "region": resource["region"],
                        "compute_seconds": str(seconds),
                        "interval_start": stamp(begin), "interval_end": stamp(end),
                        "schema_version": "1"}})
    return {"schema_version": DATASET_FORMAT_VERSION, "dataset_id": cfg["dataset_id"], "config": cfg,
            "events": events, "measurements": measurements,
            "events_sha256": digest(events), "measurements_sha256": digest(measurements)}


def validate_dataset(data, now=None, ingestion=False):
    cfg = data["config"]
    validate_config(cfg)
    if data["dataset_id"] != cfg["dataset_id"] or data.get("schema_version") != DATASET_FORMAT_VERSION:
        raise ValueError("Dataset metadata mismatch")
    if digest(data["events"]) != data["events_sha256"] or digest(data["measurements"]) != data["measurements_sha256"]:
        raise ValueError("Dataset hash mismatch; preserve the original fixture")
    # Enforce deterministic provenance, including transaction IDs and measurement pairs.
    expected = generate(cfg)
    if data != expected:
        raise ValueError("Dataset differs from its deterministic configuration")
    aliases = {c["alias"] for c in cfg["customers"]}
    seen = set()
    now = now or datetime.now(UTC)
    for event in data["events"]:
        p = event["properties"]
        if event["customer_id"] not in aliases or p["cloud_provider"] not in ("aws", "gcp"):
            raise ValueError("Unknown attribution")
        if event["transaction_id"] in seen:
            raise ValueError("Duplicate transaction ID inside fixture")
        if not 1 <= len(event["transaction_id"]) <= 128:
            raise ValueError("Transaction IDs must be 1-128 characters")
        seen.add(event["transaction_id"])
        if event["event_type"] != EVENT_TYPE or p["schema_version"] != "1":
            raise ValueError("Event schema mismatch")
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in p.items()):
            raise ValueError("All event properties must be strings")
        if not re.fullmatch(r"[1-9][0-9]*", p["compute_seconds"]):
            raise ValueError("compute_seconds must be a positive integer encoded as a string")
        seconds = int(p["compute_seconds"])
        begin, end = parse_time(p["interval_start"]), parse_time(p["interval_end"])
        if not 0 < seconds <= 3600 or end - begin != timedelta(hours=1):
            raise ValueError("Invalid measured usage interval")
        if parse_time(event["timestamp"]) != begin:
            raise ValueError("The fixture timestamps each event at interval start")
        if ingestion and (begin < now - timedelta(days=34) or end > now):
            raise ValueError("Live send requires completed windows within the 34-day ingestion window; do not retimestamp old usage")
    return len(seen)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # Never forward the bearer token to another endpoint.


def post_ingest(events, token):
    validate_token(token)
    request = Request(BASE_URL + "/v1/ingest", data=canonical(events).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"}, method="POST")
    try:
        with build_opener(NoRedirect).open(request, timeout=30) as response:
            return response.status, dict(response.headers)
    except HTTPError as exc:
        return exc.code, dict(exc.headers)
    except (URLError, TimeoutError, OSError):
        return 0, {}  # Outcome unknown; replay exactly the same IDs and payload.
    except ValueError:
        raise ValueError("Unable to construct the HTTP request; check local configuration") from None


def validate_token(token):
    if not isinstance(token, str) or not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ValueError("Bearer token is missing or has invalid characters; check it locally")


def retry_delay(headers, attempt, jitter=random.random):
    # Metronome prescribes exponential backoff for 429. Honor Retry-After when
    # present; its presence is not assumed. HTTP date values are supported.
    from email.utils import parsedate_to_datetime
    supplied = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    delay = min(60, 2 ** attempt) + jitter()
    if supplied:
        try:
            delay = max(delay, float(supplied))
        except ValueError:
            try:
                delay = max(delay, (parsedate_to_datetime(supplied) - datetime.now(UTC)).total_seconds())
            except (ValueError, TypeError):
                pass
    return max(0, delay)


def ingest(data, db_path, token=None, send=False, replay=False, now=None,
           transport=post_ingest, sleep=time.sleep, max_attempts=5):
    count = validate_dataset(data, now=now, ingestion=send)
    if not send:
        LOG.info("DRY RUN: %s events; %s batches; no network requests", count, (count + 99) // 100)
        return {"mode": "dry-run", "events": count, "batches": (count + 99) // 100}
    if not token:
        raise ValueError("Set METRONOME_BEARER_TOKEN locally; never paste it into chat")
    validate_token(token)
    if max_attempts < 1:
        raise ValueError("Retry budget must be positive")
    if not data["config"].get("metrics_confirmed"):
        raise ValueError("Confirm both streaming metrics exist before live ingestion")
    if not data["config"].get("products_confirmed"):
        raise ValueError("Confirm both products use the seconds metrics with divide-by-3600 conversion before live ingestion")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path, timeout=10)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS datasets(id TEXT PRIMARY KEY, hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS measurements(identity TEXT PRIMARY KEY, dataset_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL,
            state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, first_attempt TEXT,
            last_status INTEGER);
    """)
    accepted = skipped = 0
    try:
        # The CLI holds a file lock across this run. The SQL transaction protects
        # initial ledger registration; the CLI is the supported concurrency boundary.
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT hash FROM datasets WHERE id=?", (data["dataset_id"],)).fetchone()
        if row and row[0] != data["events_sha256"]:
            raise ValueError("Dataset ID already used with different payloads")
        db.execute("INSERT OR IGNORE INTO datasets VALUES (?,?)", (data["dataset_id"], data["events_sha256"]))
        for m in data["measurements"]:
            # Track each resource separately; a dataset rename is not new usage.
            identity = canonical([m["warehouse_id"], m["interval_start"], m["interval_end"]])
            old = db.execute("SELECT dataset_id FROM measurements WHERE identity=?", (identity,)).fetchone()
            if old and old[0] != data["dataset_id"]:
                raise ValueError("Overlapping fixture: rerun the original dataset or use genuinely new measurements")
            db.execute("INSERT OR IGNORE INTO measurements VALUES (?,?)", (identity, data["dataset_id"]))
        current = now or datetime.now(UTC)
        pending = []
        for event in data["events"]:
            tid, body_hash = event["transaction_id"], digest(event)
            row = db.execute("SELECT payload_hash,state,first_attempt FROM events WHERE id=?", (tid,)).fetchone()
            if row and row[0] != body_hash:
                raise ValueError("Transaction ID payload conflict")
            if row and row[2] and current - parse_time(row[2]) >= timedelta(days=33):
                # Conservative local margin before the documented 34-day dedup limit.
                raise ValueError("Replay too old for automatic retry; reconcile before any resend")
            if row and row[1] == "accepted" and not replay:
                skipped += 1
                continue
            if row and row[1] == "permanent_failure":
                raise ValueError("Permanent failure requires operator diagnosis; do not blind-retry")
            db.execute("INSERT OR IGNORE INTO events(id,payload_hash,state) VALUES (?,?,?)", (tid, body_hash, "pending"))
            pending.append(event)
        # Persist intent before a request can leave the process. Explicit lock file
        # in CLI prevents concurrent processes across commits and network calls.
        db.commit()
        for offset in range(0, len(pending), 100):
            batch = pending[offset:offset + 100]
            for attempt in range(max_attempts):
                ids = [(stamp(current), e["transaction_id"]) for e in batch]
                db.executemany("UPDATE events SET state='inflight',attempts=attempts+1,first_attempt=COALESCE(first_attempt,?) WHERE id=?", ids)
                db.commit()
                status, headers = transport(batch, token)
                state = "accepted" if status == 200 else ("retry_pending" if status == 0 or status == 429 or 500 <= status <= 599 else "permanent_failure")
                db.executemany("UPDATE events SET state=?,last_status=? WHERE id=?", [(state, status, e["transaction_id"]) for e in batch])
                db.commit()
                LOG.info("batch=%s count=%s attempt=%s http=%s state=%s", offset // 100 + 1, len(batch), attempt + 1, status, state)
                if state == "accepted":
                    accepted += len(batch)
                    break
                if state == "permanent_failure":
                    raise RuntimeError(f"HTTP {status}: stopped; inspect the request and sandbox logs before recovery")
                if attempt + 1 == max_attempts:
                    raise RuntimeError("Retry budget exhausted; pending events retained for an identical rerun")
                sleep(retry_delay(headers, attempt))
        return {"mode": "live-send", "accepted_this_run": accepted, "skipped_previously_accepted": skipped,
                "billing_verified": False}
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    gen = commands.add_parser("generate")
    gen.add_argument("--config", required=True)
    gen.add_argument("--out", required=True)
    check = commands.add_parser("validate")
    check.add_argument("--dataset", required=True)
    push = commands.add_parser("ingest")
    push.add_argument("--dataset", required=True)
    push.add_argument("--state", default="private/ingestion.sqlite3")
    push.add_argument("--send", action="store_true")
    push.add_argument("--replay-accepted", action="store_true", help="Controlled duplicate-delivery test using unchanged events")
    push.add_argument("--report", default="private/last-run.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if args.command == "generate":
            data = generate(load(args.config))
            validate_dataset(data)
            if Path(args.out).exists() and load(args.out) != data:
                raise ValueError("Refusing to overwrite a different dataset; use a new output file")
            save(args.out, data)
            LOG.info("Generated %s events; sha256=%s", len(data["events"]), data["events_sha256"])
        elif args.command == "validate":
            LOG.info("Validated %s deterministic events locally", validate_dataset(load(args.dataset)))
        else:
            # Advisory local process lock; automatically releases on process exit.
            # fcntl is available on the assessment's macOS/Linux environments.
            import fcntl
            Path(args.state).parent.mkdir(parents=True, exist_ok=True)
            with open(args.state + ".lock", "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                data = load(args.dataset)
                context = {"started_at": stamp(datetime.now(UTC)), "dataset_sha256": data.get("events_sha256"),
                    "billing_verified": False, "mode": "live-send" if args.send else "dry-run"}
                save(args.report, {**context, "state": "started"})
                try:
                    report = ingest(data, args.state, os.environ.get("METRONOME_BEARER_TOKEN"), args.send, args.replay_accepted)
                except Exception:
                    save(args.report, {**context, "state": "failed_or_partial", "finished_at": stamp(datetime.now(UTC)),
                        "next_step": "Inspect sanitized logs and persistent ledger; no billing verification claimed"})
                    raise
                save(args.report, {**context, **report, "state": "finished", "finished_at": stamp(datetime.now(UTC))})
    except (ValueError, KeyError, TypeError, RuntimeError, OSError, sqlite3.Error) as exc:
        LOG.error("%s", exc)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
