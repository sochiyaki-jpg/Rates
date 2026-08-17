# Rates

Publishes a small static JSON file of daily precious-metal benchmark rates.

- `docs/gold.json` is the published file, served over GitHub Pages.
- `scripts/fetch_gold.py` reads the IBJA published rates and validates them.
- The Actions job runs twice a day on weekdays and commits only when the
  numbers change.

## Why it refuses more often than it writes

A wrong rate is worse than a stale one. The script exits non-zero and leaves
the previous file in place if any purity is missing, if 999 falls outside a
sane band, if any purity drifts from its expected fraction of 999 (which
catches a re-ordered column), or if anything moved more than 15% since the
last publication. A public holiday therefore looks exactly like a failure,
which is intended.

Rates are indicative. IBJA is a wholesale benchmark; retail sits above it.
