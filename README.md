# Raindrop usage billing — Part 1

This project generates 360 synthetic warehouse-usage events for three customers and sends them to Metronome in controlled batches. An independent calculator turns the source measurements into expected hours and charges.

The source and tests are the same versions used for the assessment. The accompanying Part 1 document contains the account configuration, invoice screenshot and live validation results. This repository contains the code needed to reproduce the local calculation and tests.

## Quick start — no credentials or account requests

Use **Python 3.11 or later on macOS or Linux**. Everything uses the Python standard library; no package installation is needed. Open a terminal in this project's folder, then run:

```sh
mkdir -p scratch
python3 src/raindrop.py generate --config config.example.json --out scratch/dataset.json
python3 src/raindrop.py validate --dataset scratch/dataset.json
python3 src/raindrop.py ingest --dataset scratch/dataset.json --state scratch/ingestion.sqlite3 --report scratch/dry-run.json
python3 src/reconcile.py --dataset scratch/dataset.json --out scratch/expected-totals.json
python3 -m unittest discover -s tests -v
```

Expected dry-run message:

```text
INFO DRY RUN: 360 events; 4 batches; no network requests
```

The tests should report **22 tests passed**. Network outcomes in the tests are simulated. Generated files go into the ignored `scratch/` folder. The dry-run report records `billing_verified: false`: local validation and HTTP acceptance do not establish correct billing.

## Expected results

The fixed dataset covers 60 hourly intervals from **8 November 2026 00:00 UTC to 10 November 2026 12:00 UTC**. Each customer has one AWS warehouse and one GCP warehouse, producing 120 events per customer.

| Customer | Seconds per hourly event | Hours per provider | AWS expected USD | GCP expected USD |
|---|---:|---:|---:|---:|
| Dunder Mifflin | 900 | 15 | 8.10 | 7.35 |
| Initech | 1,800 | 30 | 16.20 | 14.70 |
| Hooli | 3,600 | 60 | 32.40 | 29.40 |

AWS is $0.54/hour; GCP is $0.49/hour. For Dunder's AWS warehouse: `60 × 900 seconds ÷ 3,600 = 15 hours`, then `15 × $0.54 = $8.10`.

The assessment's actual Dunder draft invoice was $15.45. Initech and Hooli had usage but no contracts, so their charges above are hypothetical. Local output in `examples/expected-totals.json` intentionally leaves observed usage and invoice amounts empty; it is not a captured API response.

## What to read

| File | Purpose |
|---|---|
| `src/raindrop.py` | Validates configuration, generates events and sends batches with a persistent ledger. |
| `src/reconcile.py` | Calculates expected usage from the measurement ledger using decimal arithmetic. |
| `config.example.json` | Synthetic customer aliases, warehouses, durations and dates; live-send confirmation flags start false. |
| `tests/test_billing.py` | Existing 22-test suite covering generation, retries, reruns, attribution and reconciliation. |
| `examples/sample-event.json` | One event from the generated fixture. |
| `examples/expected-totals.json` | Six expected customer/provider rows generated locally. |
| `validation/local-checks.txt` | Output from checking this source package, including the test run. |

## Event model and delivery behaviour

- Each event records a warehouse's incremental running time within a completed hourly UTC interval. `compute_seconds` is an integer encoded as a string. Region and size describe the warehouse; they do not change the rate in this exercise.
- `transaction_id` combines `warehouse_compute`, the globally unique warehouse ID and the interval start. Repeating the same configuration produces the same IDs and payloads. Changing a dataset label does not create new usage.
- Metronome metrics sum seconds; products divide by 3,600 before pricing. The independent calculator reads measurement records rather than the event properties or invoice under comparison.
- Live requests use `POST /v1/ingest`, at most 100 events per request, a 30-second timeout and up to five attempts per batch. Timeouts, 429 and 5xx responses retry the same payload with backoff; other HTTP failures stop for investigation.
- SQLite records progress. Reusing the ledger skips accepted events on ordinary reruns and rejects conflicting payloads. Exhausted retries retain pending state. A file lock allows one CLI sender at a time.

## Live mode — optional

The completed assessment does not need another live send. The quick start above is sufficient for reviewing the package.

For a separately prepared account, live mode requires all of the following:

1. Copy `config.example.json` to `private/config.json` and confirm the customer aliases match the intended account. Create the metrics and product conversions before ingestion; configure rates and the customer contract for invoice validation.
2. Set `metrics_confirmed` and `products_confirmed` to `true` in that local copy only after checking the setup. These flags are operator confirmations, not automatic account checks. The local `environment` label does not select the destination account; the API token does.
3. Supply `METRONOME_BEARER_TOKEN` through your local environment or secret manager. Keep credentials out of configuration files, source control and command history.
4. Generate and validate the local dataset, then explicitly opt into sending:

```sh
python3 src/raindrop.py generate --config private/config.json --out private/dataset.json
python3 src/raindrop.py validate --dataset private/dataset.json
python3 src/raindrop.py ingest --dataset private/dataset.json --state private/ingestion.sqlite3 --report private/live-run.json --send
```

The fixed dates remain valid for local reproduction. Live sending also checks completed intervals and the documented 34-day ingestion window, so old fixture dates will eventually be rejected. The ledger applies a conservative 33-day replay guard. New live data must represent genuinely new measurements; do not retimestamp previously delivered usage or discard its ledger just to rerun a demonstration.

After a live send, verify customer attribution, metric matching, usage totals and invoice line items separately. HTTP 200 alone is not billing reconciliation.

## Validation scope

The assessment's recorded live run returned HTTP 200 for four batches of 100, 100, 100 and 60 events. It replayed 200 events previously sent in Postman and added 160 new events. Checked usage matched all six expected totals, and Dunder's draft invoice remained $15.45; the accompanying document holds that evidence.

The repository tests use mock network responses for retryable, permanent and ambiguous failures. The optional captured-invoice parser in `reconcile.py` has synthetic test coverage; no raw invoice response from the assessment account is included. Final invoice quantities are whole hours, so this fixture does not establish Metronome's handling of fractional final quantities or sub-cent rounding. Running time is an exercise assumption; billing idle, provisioned time would need agreement before launch.

## References

- [Event structure and retry guidance](https://docs.metronome.com/guides/events/send-usage-events)
- [Ingest API v1](https://docs.metronome.com/api-reference/usage/ingest-events)
- [Billable metrics and group keys](https://docs.metronome.com/guides/implement-metronome/core-concepts/create-billable-metrics)
- [Products and quantity conversion](https://docs.metronome.com/guides/implement-metronome/core-concepts/create-products-contracts)
