#!/usr/bin/env python3
"""Independent local measurement calculation; optional captured-invoice check.

No network calls. Expected usage comes from integer measurement seconds, not
the generated event properties or the invoice under test. Metric usage is seconds;
product/invoice usage is hours after division by 3600.
"""
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
import argparse
import json
from pathlib import Path
from datetime import datetime, timezone
from raindrop import validate_dataset

RATES = {"aws": Decimal("0.54"), "gcp": Decimal("0.49")}
USD_CENTS_ID = "2714e483-4ff1-48e4-9e25-ac732e8f24f2"


def utc(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Evidence timestamps need an explicit timezone")
    return parsed.astimezone(timezone.utc)


def expected(data):
    seconds = defaultdict(int)
    seen = {}
    for m in data["measurements"]:
        tid = m["transaction_id"]
        if tid in seen:
            if seen[tid] != m:
                raise ValueError("Conflicting measurement identity")
            continue
        seen[tid] = m
        if type(m["active_seconds"]) is not int or not 0 < m["active_seconds"] <= 3600:
            raise ValueError("Invalid integer seconds")
        if m["cloud_provider"] not in RATES:
            raise ValueError("Unknown provider")
        seconds[m["customer_alias"], m["cloud_provider"]] += m["active_seconds"]
    result = []
    for (alias, provider), value in sorted(seconds.items()):
        hours = Decimal(value) / Decimal(3600)
        raw = hours * RATES[provider]
        result.append({"customer": alias, "provider": provider,
            "expected_usage_seconds": str(value), "observed_usage_seconds": None,
            "expected_usage_hours": str(hours), "observed_usage_hours": None,
            "rate_usd_per_hour": str(RATES[provider]),
            "expected_charge_usd": str(raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            "unrounded_charge_usd": str(raw), "actual_invoice_charge_usd": None,
            "difference_usd": None, "evidence": "local measurement ledger only; no sandbox observation"})
    return result


def read_plain_invoice(response, mapping):
    inv = response["data"]
    if inv["status"] not in ("DRAFT", "FINALIZED") or inv["type"] != "USAGE":
        raise ValueError("Only draft/finalized plain usage invoices are supported")
    if inv["customer_id"] != mapping["customer_id"] or inv["contract_id"] != mapping["contract_id"]:
        raise ValueError("Invoice customer/contract mismatch")
    if utc(inv["start_timestamp"]) != utc(mapping["start_timestamp"]) or utc(inv["end_timestamp"]) != utc(mapping["end_timestamp"]):
        raise ValueError("Billing period mismatch; normalize timestamps explicitly in the mapping")
    if inv["credit_type"]["id"] != USD_CENTS_ID:
        raise ValueError("Unsupported denomination; this checker only supports USD cents")
    lines = defaultdict(lambda: {"quantity": Decimal(0), "total_cents": Decimal(0)})
    products = mapping["products"]
    if set(products.values()) != {"aws", "gcp"} or len(products) != 2:
        raise ValueError("Supply exactly two verified product IDs mapped to aws/gcp")
    for line in inv["line_items"]:
        if any(line.get(k) is not None for k in ("commit_id", "credit_grant_id", "discount_id", "applied_commit_or_credit", "postpaid_commit")):
            raise ValueError("Credit/commit/discount line requires explicit review")
        if line.get("credit_type", {}).get("id") != USD_CENTS_ID:
            raise ValueError("Line currency missing or unsupported")
        if line["product_id"] not in products or line.get("product_type") != "UsageProductListItem":
            raise ValueError("Unrecognized product or line type; no silent skipping")
        provider = products[line["product_id"]]
        q, total, price = (Decimal(str(line[k])) for k in ("quantity", "total", "unit_price"))
        if not all(x.is_finite() and x >= 0 for x in (q, total, price)):
            raise ValueError("Invalid line amounts")
        if price != RATES[provider] * 100 or q * price != total:
            raise ValueError("Price or line arithmetic differs from simple assessment model")
        lines[provider]["quantity"] += q
        lines[provider]["total_cents"] += total
    total = sum(x["total_cents"] for x in lines.values())
    if total != Decimal(str(inv["total"])):
        raise ValueError("Invoice total does not equal supported plain usage lines")
    return inv, lines


def compare(data, response=None, mapping=None, observations=None):
    validate_dataset(data)
    rows = expected(data)
    if len(rows) != 6:
        raise ValueError("Expected all six customer/provider measurement groups")
    if observations:
        allowed = {r["customer"] for r in rows}
        if set(observations) - allowed:
            raise ValueError("Unknown customer in usage observations")
        for alias, values in observations.items():
            if set(values) - {"aws", "gcp"}:
                raise ValueError("Unknown provider in usage observations")
        for row in rows:
            obs = observations.get(row["customer"], {}).get(row["provider"])
            if obs:
                if not obs.get("source_file") or not obs.get("query_start") or not obs.get("query_end"):
                    raise ValueError("Observed usage requires source and query period")
                if utc(obs["query_start"]) > utc(data["config"]["start"]) or utc(obs["query_end"]) < max(utc(m["interval_end"]) for m in data["measurements"]):
                    raise ValueError("Observed usage query does not cover every measurement")
                if ("seconds" in obs) == ("hours" in obs):
                    raise ValueError("Observed usage needs exactly one explicit unit: seconds or hours")
                amount = Decimal(str(obs["seconds"] if "seconds" in obs else obs["hours"]))
                actual_seconds = amount if "seconds" in obs else amount * 3600
                if not actual_seconds.is_finite() or actual_seconds != Decimal(row["expected_usage_seconds"]):
                    raise ValueError("Observed usage differs; investigate before claiming reconciliation")
                row["observed_usage_seconds"] = str(actual_seconds)
                row["observed_usage_hours"] = str(actual_seconds / 3600)
                row["evidence"] = "Usage observation: " + obs["source_file"] + "; invoice not yet captured"
    if response is None:
        return rows
    if mapping["customer_alias"] not in {r["customer"] for r in rows}:
        raise ValueError("Invoice mapping alias is absent from the fixture")
    if utc(mapping["start_timestamp"]) != utc(data["config"]["period_start"]) or utc(mapping["end_timestamp"]) != utc(data["config"]["period_end"]):
        raise ValueError("Invoice mapping period does not match fixture period")
    inv, lines = read_plain_invoice(response, mapping)
    # observations are separately recorded usage query results, not invoice quantity.
    for row in rows:
        if row["customer"] != mapping["customer_alias"]:
            row["evidence"] += "; no invoice checked for this customer; charge is hypothetical"
            continue
        actual = lines.get(row["provider"], {"quantity": Decimal(0), "total_cents": Decimal(0)})
        if actual["quantity"] != Decimal(row["expected_usage_hours"]):
            raise ValueError("Invoice quantity differs from independent measurements")
        charge = actual["total_cents"] / 100
        row["actual_invoice_charge_usd"] = str(charge)
        row["difference_usd"] = str(charge - Decimal(row["expected_charge_usd"]))
        row["evidence"] += f"; captured invoice {inv['id']}; status {inv['status']}; capture provenance must be retained"
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", required=True)
    p.add_argument("--invoice")
    p.add_argument("--mapping")
    p.add_argument("--observations")
    p.add_argument("--out", required=True)
    args = p.parse_args()
    def read(path):
        return json.loads(Path(path).read_text(), parse_float=Decimal) if path else None
    if bool(args.invoice) != bool(args.mapping):
        p.error("--invoice and --mapping must be supplied together")
    rows = compare(read(args.dataset), read(args.invoice), read(args.mapping), read(args.observations))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(rows, indent=2) + "\n")
    print("Wrote local expected results" if not args.invoice else "Compared captured invoice; review provenance and missing usage observations")


if __name__ == "__main__":
    main()
