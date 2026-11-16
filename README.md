# Raindrop usage billing — Part 1

One Python script generates 360 sample warehouse-usage events, calculates expected totals and optionally sends the events to Metronome. It uses Python 3.11 or later and the standard library; no packages need installing.

## Run locally

After cloning or downloading the repository, open a terminal in its folder and run:

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
3. `expected_totals()` calculates expected hours and charges from the configured running times and hourly rates, using decimal arithmetic. These rates calculate local expectations; Metronome applies the prices in its rate card.
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

For Dunder's AWS warehouse: `60 × 900 ÷ 3,600 = 15 hours`, then `15 × $0.54 = $8.10`. GCP is $0.49/hour. The table shows calculated expectations. Dunder Mifflin’s combined $15.45 was checked against its draft invoice. Initech and Hooli have no contracts, so their charges are hypothetical.

## Delivery and recovery

Requests use `POST /v1/ingest`, a 30-second timeout and at most five attempts per batch per run. Network errors, HTTP 429 and 5xx responses retry with increasing delays and a small random delay (jitter). A valid `Retry-After` header is respected. Other HTTP failures stop immediately. Requests do not follow redirects.

A rerun resends all 360 events with the same transaction IDs; there is no local delivery database. Metronome ignores repeated IDs for 34 days after acceptance. If a run stops partway, check the reported batch and account usage, then rerun the unchanged configuration once the issue is resolved. Keep IDs, timestamps and usage quantities unchanged; reusing an ID does not update accepted usage.

Live mode rejects unfinished hourly windows and event timestamps more than 34 days old. This event-age check is separate from duplicate detection, which runs from acceptance.

HTTP 200 confirms acceptance, not correct billing. Check customer attribution, provider usage and invoice line items separately.

## Sending usage to Metronome

The script generates and saves the events by default. Add `--send` to send those events to Metronome. Sending requires an API token and the customer and billing configuration described in the assessment document.

- Use the supplied `config.example.json` for the assessment account. For another account, copy it to `config.local.json` (ignored by Git) and edit the copy.
- Confirm the customer aliases, metrics, seconds-to-hours product conversions, rates and contract in the destination account. The script does not check this setup through the API.
- Load `METRONOME_BEARER_TOKEN` through your local environment or secret manager. The token selects the destination account. Keep it out of files, source control and command history.

With the supplied configuration, first inspect the dry-run output, then send:

```sh
python3 src/raindrop.py
python3 src/raindrop.py --send
```

To use a local configuration, add `--config config.local.json` to either command.

The fixed example dates remain usable for dry runs after they become too old for live ingestion. New dates must represent new usage.

## Validation and limits

**Local checks:** The repository script passes 13 tests and reproduces the assessment’s 360 events, including IDs, timestamps and properties. Its delivery behaviour has been tested with simulated API responses, not a live send.

**Account results:** The assessment live run on 11 November returned HTTP 200 for all four batches. It resent 200 earlier Postman events unchanged and added 160 new ones. All six checked usage totals matched expectations, and Dunder Mifflin’s draft invoice remained $15.45.

Final invoice quantities are whole hours. Live invoice validation did not cover fractional final quantities or sub-cent rounding.

## References

- [Event structure, duplicate handling and retries](https://docs.metronome.com/guides/events/send-usage-events)
- [Ingest API and batch limits](https://docs.metronome.com/api-reference/usage/ingest-events)
