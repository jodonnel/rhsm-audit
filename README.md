# RHSM Subscription Audit Tool

Reconciles registered RHEL systems against purchased subscriptions using the Red Hat Subscription Management API. Built for infrastructure leads who need to find stale systems, untagged vendor-dropped boxes, and entitlement overages.

## Setup

1. Go to **https://access.redhat.com/management/api**
2. Click **Generate Token** to get an offline API token
3. Export it:

```bash
export RHSM_OFFLINE_TOKEN="your-token-here"
```

The token lasts 30 days. Regenerate when it expires.

## Usage

```bash
# Default: 90-day stale threshold
python3 rhsm-audit.py

# Custom stale threshold
python3 rhsm-audit.py --days 60

# Faster run (skip per-system detail fetch — no system purpose data)
python3 rhsm-audit.py --skip-enrich

# Output to a specific directory
python3 rhsm-audit.py --output-dir /tmp/audit
```

## Live Dashboard

```bash
# Run the Flask dashboard (auto-refreshes from the RHSM API)
RHSM_OFFLINE_TOKEN=$(cat ~/.rhsm-offline-token) python3 dashboard.py
# Open http://localhost:8099

# Or run in a container
podman build -t rhsm-dashboard .
podman run -p 8099:8099 -e RHSM_OFFLINE_TOKEN=$(cat ~/.rhsm-offline-token) rhsm-dashboard
```

## Outputs

| File | Contents |
|------|----------|
| `rhsm-audit-report-YYYY-MM-DD.html` | Styled HTML report with executive summary, tables, recommendations |
| `rhsm-audit-all-systems-YYYY-MM-DD.csv` | Every registered system |
| `rhsm-audit-stale-systems-YYYY-MM-DD.csv` | Systems not checked in for N+ days |
| `rhsm-audit-untagged-systems-YYYY-MM-DD.csv` | Systems with no system purpose role/usage/SLA |

## Requirements

- Python 3.6+
- `requests` (pre-installed on RHEL 8/9)
- `flask` (dashboard only)

## Notes

- The enrichment step fetches detail for each system individually (the list endpoint does not include system purpose data). For large estates (1000+ systems), use `--skip-enrich` for a fast first pass, then enrich selectively.
- The API rate limits are generous but the script adds small delays between calls to be polite.
- Entitlement delta = purchased - consumed. Negative means overconsumed.
