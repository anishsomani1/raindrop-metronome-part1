# Raindrop usage billing — Part 1

One Python script generates 360 sample warehouse-usage events, calculates expected totals and optionally sends the events to Metronome. It uses Python 3.11 or later and the standard library; no packages need installing.

This is the simplified submission version. Its 360 events were compared with the preserved original implementation and match exactly, including IDs, timestamps and properties. This revision was validated locally and has not been sent to Metronome. The invoice and account screenshots in the document come from the original live run.

## Start here

From the repository folder, run:

```sh
python3 src/raindrop.py
python3 -m unittest discover -s tests -v
```

The first command is a dry run: it needs no token and makes no network requests. It writes `scratch/events.json` and `scratch/expected-totals.json`, then prints:

```text
DRY RUN: 360 events; 4 batches; no network requests
```

The second command runs 13 local tests. API responses in these tests are simulated. Generated files are ignored by Git.

## How the script works

1. `generate()` checks the configuration and loops over three customers, two providers and 60 hourly intervals.
2. Each event reports actual running seconds within its hour. The transaction ID combines the event type, warehouse ID and interval start. The same configuration produces the same events.
3. `expected_totals()` calculates hours and charges from the configured durations using decimal arithmetic, independently of the event JSON.
4. Dry run saves the generated data for inspection. Explicit `--send` enables delivery through `send_events()`.
5. The sender posts batches of 100, 100, 100 and 60 events. It retries temporary failures with unchanged payloads and IDs, and stops on errors that need investigation.

`config.example.json` holds the customer aliases, warehouse details, durations and dates. `tests/test_billing.py` checks generation, totals, validation and delivery behaviour. `examples/` contains one event and the expected totals; `validation/local-checks.txt` records local verification.

## Dataset and expected results

There are 60 hourly intervals from **8 November 2026 at 00:00 UTC to 10 November at 12:00 UTC**. Each customer has one AWS warehouse and one GCP warehouse, giving 120 events per customer. Whole seconds are encoded as strings. Warehouse region and size do not change the price in this example.

| Customer | Seconds per hourly event | Hours per provider | AWS expected USD | GCP expected USD |
|---|---:|---:|---:|---:|
| Dunder Mifflin | 900 | 15 | 8.10 | 7.35 |
| Initech | 1,800 | 30 | 16.20 | 14.70 |
| Hooli | 3,600 | 60 | 32.40 | 29.40 |

For Dunder's AWS warehouse: `60 × 900 ÷ 3,600 = 15 hours`, then `15 × $0.54 = $8.10`. GCP is $0.49/hour. Initech and Hooli have no contracts, so their charges above are hypothetical. Expected totals are local calculations, not observed account results.

## Delivery and recovery

Requests use `POST /v1/ingest`, a 30-second timeout and at most five attempts per batch per run. Network errors, HTTP 429 and 5xx responses retry with increasing delays and jitter. A valid `Retry-After` header is respected. Other HTTP failures stop immediately. Requests do not follow redirects.

There is no local delivery database. A rerun regenerates and resends all 360 events. Within Metronome's documented 34-day duplicate-detection window after acceptance, unchanged transaction IDs let Metronome ignore repeated events. Do not change IDs, timestamps or quantities to retry old usage. A changed amount under the same ID is not an update to accepted usage.

If a run stops, some events may already have arrived. Inspect the reported batch and account usage before rerunning the unchanged configuration. Do not assume duplicate protection lasts indefinitely. Live mode rejects incomplete windows and events older than the 34-day ingestion window; the ingestion and duplicate-detection windows are separate rules.

HTTP 200 confirms acceptance, not correct billing. Check customer attribution, provider usage and invoice line items separately.

## Optional live mode

Reviewing the completed assessment only requires the dry run and tests. For a separate live exercise:

- Confirm the customer aliases, metrics and seconds-to-hours product conversions in the intended account. Configure the rates and contract needed for invoice validation.
- Save any local changes in the ignored `config.local.json`; the script does not inspect Metronome configuration automatically.
- Load `METRONOME_BEARER_TOKEN` through your local environment or secret manager. The token selects the destination account. Keep it out of files, source control and command history.
- Inspect the dry-run data before explicitly sending:

```sh
python3 src/raindrop.py --config config.local.json
python3 src/raindrop.py --config config.local.json --send
```

The fixed dates always work for local review but eventually become too old for live ingestion. New dates must represent genuinely new usage; do not retimestamp the assessment events for another demonstration.

## Validation and scope

The original live run returned HTTP 200 for all four batches. It resent 200 earlier Postman events and added 160 new ones. All six checked usage totals matched expectations, and Dunder's draft invoice remained $15.45. The original source and run evidence are preserved separately with the assessment materials.

The simplified version passes 13 local tests and reproduces the original 360 event objects and six expected totals. Failure and rerun tests use simulated API responses; they are not additional live evidence. The script intentionally has no delivery ledger, file lock, dataset fingerprints or captured-invoice parser.

Final invoice quantities in this dataset are whole hours. The live evidence does not establish fractional final quantities or sub-cent rounding. Billing idle, provisioned time would also need agreement with Raindrop.

## References

- [Event structure, duplicate handling and retries](https://docs.metronome.com/guides/events/send-usage-events)
- [Ingest API and batch limits](https://docs.metronome.com/api-reference/usage/ingest-events)
