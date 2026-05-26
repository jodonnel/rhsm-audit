#!/usr/bin/env python3
"""
RHSM Subscription Audit Tool
=============================
Reconciles registered RHEL systems against purchased subscriptions using
the Red Hat Subscription Management (RHSM) API.

Built to identify:
  - Stale systems (not checked in for N days) still consuming entitlements
  - Untagged systems (no system purpose role/usage/SLA) — likely vendor-dropped
    boxes (GE medical appliances, Wintel ESX hosts with VDC licenses)
  - Subscription allocation gaps (purchased vs. consumed delta)

Prerequisites:
  1. Generate an offline token at https://access.redhat.com/management/api
  2. Export it:  export RHSM_OFFLINE_TOKEN="your-token-here"
  3. Run:       python3 rhsm-audit.py [--days 90]

Outputs:
  - rhsm-audit-report-YYYY-MM-DD.html   (styled HTML report)
  - rhsm-audit-all-systems-YYYY-MM-DD.csv
  - rhsm-audit-stale-systems-YYYY-MM-DD.csv
  - rhsm-audit-untagged-systems-YYYY-MM-DD.csv

Requires: requests (standard on RHEL 8/9)
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

try:
    import requests
except ImportError:
    print("ERROR: 'requests' library not found. Install with: pip3 install requests", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SSO_TOKEN_URL = "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token"
API_BASE = "https://api.access.redhat.com/management/v1"
SYSTEMS_PAGE_SIZE = 100
SUBSCRIPTIONS_PAGE_SIZE = 50
DEFAULT_STALE_DAYS = 90


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def get_access_token(offline_token: str) -> str:
    """Exchange an offline token for a short-lived access token."""
    resp = requests.post(SSO_TOKEN_URL, data={
        "grant_type": "refresh_token",
        "client_id": "rhsm-api",
        "refresh_token": offline_token,
    }, timeout=30)

    if resp.status_code == 400:
        body = resp.json()
        desc = body.get("error_description", body.get("error", "unknown"))
        if "expired" in desc.lower() or "invalid" in desc.lower():
            print(f"ERROR: Offline token is expired or invalid: {desc}", file=sys.stderr)
            print("       Generate a new one at https://access.redhat.com/management/api", file=sys.stderr)
        else:
            print(f"ERROR: Token exchange failed (400): {desc}", file=sys.stderr)
        sys.exit(1)

    if resp.status_code != 200:
        print(f"ERROR: Token exchange failed (HTTP {resp.status_code}): {resp.text[:500]}", file=sys.stderr)
        sys.exit(1)

    return resp.json()["access_token"]


def api_headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------
def api_get(access_token: str, path: str, params: Optional[dict] = None) -> dict:
    """GET from the RHSM API with basic error handling."""
    url = f"{API_BASE}{path}"
    resp = requests.get(url, headers=api_headers(access_token), params=params or {}, timeout=60)

    if resp.status_code == 401:
        print("ERROR: Authentication failed (401). Token may have expired mid-run.", file=sys.stderr)
        sys.exit(1)
    if resp.status_code == 403:
        print(f"ERROR: Forbidden (403) on {path}. Check account permissions.", file=sys.stderr)
        sys.exit(1)
    if resp.status_code != 200:
        print(f"ERROR: API returned HTTP {resp.status_code} for {path}: {resp.text[:300]}", file=sys.stderr)
        sys.exit(1)

    return resp.json()


def paginate(access_token: str, path: str, page_size: int, extra_params: Optional[dict] = None) -> list:
    """Fetch all pages from a paginated RHSM endpoint."""
    results = []
    offset = 0
    while True:
        params = {"limit": page_size, "offset": offset}
        if extra_params:
            params.update(extra_params)
        data = api_get(access_token, path, params)
        body = data.get("body", [])
        if not body:
            break
        results.extend(body)
        pagination = data.get("pagination", {})
        count = pagination.get("count", len(body))
        if count < page_size:
            break
        offset += page_size
        # Be polite to the API
        time.sleep(0.25)
    return results


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------
def fetch_all_systems(access_token: str) -> list:
    """Fetch all registered systems (basic list endpoint)."""
    print("  Fetching registered systems...", flush=True)
    systems = paginate(access_token, "/systems", SYSTEMS_PAGE_SIZE)
    print(f"  Found {len(systems)} registered system(s).")
    return systems


def fetch_system_details(access_token: str, uuid: str) -> dict:
    """Fetch detailed info for a single system (includes system purpose, facts)."""
    data = api_get(access_token, f"/systems/{uuid}", {"include": "facts,entitlements,installedProducts"})
    return data.get("body", {})


def enrich_systems(access_token: str, systems: list) -> list:
    """Pull system detail for each system to get system purpose and created date."""
    print(f"  Enriching {len(systems)} systems with detail data (this may take a while)...", flush=True)
    enriched = []
    for i, s in enumerate(systems):
        uuid = s.get("uuid", "")
        if not uuid:
            enriched.append(s)
            continue
        try:
            detail = fetch_system_details(access_token, uuid)
            # Merge detail into system record
            merged = {**s, **detail}
            enriched.append(merged)
        except Exception as e:
            print(f"    Warning: could not fetch detail for {s.get('hostname', uuid)}: {e}", file=sys.stderr)
            enriched.append(s)
        if (i + 1) % 25 == 0:
            print(f"    ...{i+1}/{len(systems)}", flush=True)
        time.sleep(0.2)
    return enriched


def fetch_subscriptions(access_token: str) -> list:
    """Fetch all subscription allocations."""
    print("  Fetching subscriptions...", flush=True)
    subs = paginate(access_token, "/subscriptions", SUBSCRIPTIONS_PAGE_SIZE)
    print(f"  Found {len(subs)} subscription(s).")
    return subs


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
def parse_date(datestr: Optional[str]) -> Optional[datetime]:
    """Parse RHSM date string to datetime."""
    if not datestr:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(datestr, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def analyze_systems(systems: list, stale_days: int):
    """Classify systems as stale and/or untagged."""
    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(days=stale_days)

    stale = []
    untagged = []

    for s in systems:
        last_checkin = parse_date(s.get("lastCheckin"))
        days_since = None
        if last_checkin:
            days_since = (now - last_checkin).days

        # Stale: no check-in or check-in older than threshold
        if last_checkin is None or last_checkin < stale_cutoff:
            s["_days_since_checkin"] = days_since if days_since is not None else "never"
            stale.append(s)

        # Untagged: no system purpose role, usage, or SLA
        role = s.get("systemPurposeRole") or s.get("syspurposeRole") or ""
        usage = s.get("systemPurposeUsage") or s.get("syspurposeUsage") or ""
        sla = s.get("serviceLevelPreference") or s.get("systemPurposeSLA") or ""

        # Also check facts for system purpose
        facts = s.get("facts", {})
        if isinstance(facts, list):
            facts_dict = {f.get("key", ""): f.get("value", "") for f in facts if isinstance(f, dict)}
        elif isinstance(facts, dict):
            facts_dict = facts
        else:
            facts_dict = {}

        if not role:
            role = facts_dict.get("system_purpose_role", "")
        if not usage:
            usage = facts_dict.get("system_purpose_usage", "")
        if not sla:
            sla = facts_dict.get("system_purpose_sla", "")

        s["_purpose_role"] = role
        s["_purpose_usage"] = usage
        s["_purpose_sla"] = sla

        if not role and not usage and not sla:
            s["_is_untagged"] = True
            untagged.append(s)
        else:
            s["_is_untagged"] = False

    return stale, untagged


def analyze_subscriptions(subs: list):
    """Build subscription allocation summary with purchased/consumed delta."""
    rows = []
    for sub in subs:
        name = sub.get("subscriptionName", "Unknown")
        sku = sub.get("sku", "")
        status = sub.get("status", "")
        purchased = 0
        consumed = 0
        pools = sub.get("pools", [])
        for pool in pools:
            purchased += pool.get("quantity", 0)
            consumed += pool.get("consumed", 0)

        rows.append({
            "name": name,
            "sku": sku,
            "status": status,
            "purchased": purchased,
            "consumed": consumed,
            "delta": purchased - consumed,
        })
    return rows


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------
def write_all_systems_csv(systems: list, filepath: str):
    """Write full systems list to CSV."""
    fields = ["hostname", "name", "uuid", "type", "entitlementStatus", "entitlementCount",
              "lastCheckin", "createdDate", "_purpose_role", "_purpose_usage", "_purpose_sla",
              "_is_untagged", "username"]
    with open(filepath, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for s in systems:
            row = {k: s.get(k, "") for k in fields}
            w.writerow(row)
    print(f"  Wrote {filepath}")


def write_stale_csv(stale: list, filepath: str):
    fields = ["hostname", "name", "uuid", "lastCheckin", "_days_since_checkin",
              "_purpose_role", "_purpose_usage", "_purpose_sla", "type", "username"]
    with open(filepath, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for s in stale:
            row = {k: s.get(k, "") for k in fields}
            w.writerow(row)
    print(f"  Wrote {filepath}")


def write_untagged_csv(untagged: list, filepath: str):
    fields = ["hostname", "name", "uuid", "createdDate", "lastCheckin",
              "type", "entitlementStatus", "username"]
    with open(filepath, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for s in untagged:
            row = {k: s.get(k, "") for k in fields}
            w.writerow(row)
    print(f"  Wrote {filepath}")


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------
def generate_html(systems, stale, untagged, sub_rows, stale_days, report_path):
    """Generate the styled HTML audit report."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    datestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    total_registered = len(systems)
    total_stale = len(stale)
    total_untagged = len(untagged)
    total_purchased = sum(r["purchased"] for r in sub_rows)
    total_consumed = sum(r["consumed"] for r in sub_rows)
    total_delta = total_purchased - total_consumed

    def esc(val):
        if val is None:
            return ""
        return str(val).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def fmt_date(datestr):
        dt = parse_date(datestr)
        if dt is None:
            return "never"
        return dt.strftime("%Y-%m-%d")

    # Build subscription table rows
    sub_table = ""
    for r in sorted(sub_rows, key=lambda x: x["delta"]):
        delta_class = "cell-warn" if r["delta"] < 0 else "cell-ok" if r["delta"] > 0 else ""
        sub_table += f"""<tr>
            <td>{esc(r['name'])}</td>
            <td>{esc(r['sku'])}</td>
            <td>{esc(r['status'])}</td>
            <td class="num">{r['purchased']}</td>
            <td class="num">{r['consumed']}</td>
            <td class="num {delta_class}">{r['delta']:+d}</td>
        </tr>\n"""

    # Stale systems table
    stale_table = ""
    stale_sorted = sorted(stale, key=lambda s: s.get("_days_since_checkin") if isinstance(s.get("_days_since_checkin"), int) else 99999, reverse=True)
    for s in stale_sorted:
        days = s.get("_days_since_checkin", "never")
        purpose = " / ".join(filter(None, [s.get("_purpose_role", ""), s.get("_purpose_usage", ""), s.get("_purpose_sla", "")]))
        stale_table += f"""<tr>
            <td>{esc(s.get('hostname') or s.get('name', ''))}</td>
            <td>{fmt_date(s.get('lastCheckin'))}</td>
            <td class="num">{days}</td>
            <td>{esc(purpose) or '<span class="muted">none</span>'}</td>
            <td>{esc(s.get('type', ''))}</td>
        </tr>\n"""

    # Untagged systems table
    untagged_table = ""
    for s in untagged:
        untagged_table += f"""<tr>
            <td>{esc(s.get('hostname') or s.get('name', ''))}</td>
            <td>{fmt_date(s.get('createdDate'))}</td>
            <td>{fmt_date(s.get('lastCheckin'))}</td>
            <td>{esc(s.get('type', ''))}</td>
            <td>{esc(s.get('entitlementStatus', ''))}</td>
        </tr>\n"""

    # Overconsumed subscriptions for recommendations
    overconsumed = [r for r in sub_rows if r["delta"] < 0]
    overconsumed_note = ""
    if overconsumed:
        overconsumed_note = "<li><strong>Overconsumed subscriptions detected.</strong> The following SKUs have more consumed entitlements than purchased:<ul>"
        for r in overconsumed:
            overconsumed_note += f"<li>{esc(r['name'])} ({esc(r['sku'])}): {r['consumed']} consumed / {r['purchased']} purchased (deficit: {abs(r['delta'])})</li>"
        overconsumed_note += "</ul></li>"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RHSM Subscription Audit Report — {datestamp}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Red+Hat+Display:wght@400;500;700&family=Red+Hat+Text:wght@400;500;700&family=Red+Hat+Mono:wght@400;500&display=swap');

  html {{ scroll-behavior: smooth; }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}

  body {{
    background: #06090F;
    color: #C9D1D9;
    font-family: 'Red Hat Text', 'Segoe UI', system-ui, sans-serif;
    font-size: 15px;
    line-height: 1.6;
    padding: 2rem;
  }}

  h1, h2, h3 {{
    font-family: 'Red Hat Display', 'Segoe UI', system-ui, sans-serif;
    color: #E6EDF3;
  }}

  h1 {{
    font-size: 1.8rem;
    margin-bottom: 0.25rem;
    border-bottom: 2px solid #EE0000;
    padding-bottom: 0.5rem;
  }}

  .subtitle {{
    color: #8B949E;
    font-size: 0.9rem;
    margin-bottom: 2rem;
  }}

  h2 {{
    font-size: 1.3rem;
    margin: 2rem 0 1rem 0;
    color: #58A6FF;
  }}

  .summary-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 1rem;
    margin: 1.5rem 0;
  }}

  a.card-link {{
    text-decoration: none;
    color: inherit;
    display: block;
  }}

  .summary-card {{
    background: #0D1117;
    border: 1px solid #21262D;
    border-radius: 8px;
    padding: 1.25rem;
    text-align: center;
    cursor: pointer;
    transition: transform 0.15s ease, border-color 0.15s ease;
  }}

  .summary-card:hover {{
    transform: translateY(-3px);
    border-color: #58A6FF;
  }}

  .summary-card .big-num {{
    font-family: 'Red Hat Mono', monospace;
    font-size: 2.2rem;
    font-weight: 700;
    color: #E6EDF3;
    display: block;
  }}

  .summary-card .label {{
    font-size: 0.8rem;
    color: #8B949E;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }}

  .summary-card.warn .big-num {{ color: #D29922; }}
  .summary-card.alert .big-num {{ color: #EE0000; }}
  .summary-card.ok .big-num {{ color: #58A6FF; }}

  table {{
    width: 100%;
    border-collapse: collapse;
    background: #0D1117;
    border: 1px solid #21262D;
    border-radius: 8px;
    overflow: hidden;
    margin-bottom: 1rem;
    font-size: 0.9rem;
  }}

  thead {{ background: #161B22; }}
  th {{
    text-align: left;
    padding: 0.75rem 1rem;
    font-family: 'Red Hat Display', sans-serif;
    font-weight: 600;
    color: #8B949E;
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    border-bottom: 1px solid #21262D;
  }}

  td {{
    padding: 0.6rem 1rem;
    border-bottom: 1px solid #21262D;
    font-family: 'Red Hat Mono', monospace;
    font-size: 0.85rem;
  }}

  tr:last-child td {{ border-bottom: none; }}
  tr:hover {{ background: #161B22; }}

  .num {{ text-align: right; }}
  .cell-warn {{ color: #D29922; font-weight: 600; }}
  .cell-ok {{ color: #58A6FF; }}
  .muted {{ color: #484F58; font-style: italic; }}

  .recommendations {{
    background: #0D1117;
    border: 1px solid #21262D;
    border-left: 4px solid #58A6FF;
    border-radius: 8px;
    padding: 1.5rem;
    margin: 1.5rem 0;
  }}

  .recommendations li {{
    margin: 0.5rem 0 0.5rem 1.5rem;
  }}

  .footer {{
    margin-top: 3rem;
    padding-top: 1rem;
    border-top: 1px solid #21262D;
    color: #484F58;
    font-size: 0.8rem;
    text-align: center;
  }}

  .section-count {{
    color: #8B949E;
    font-size: 0.9rem;
    font-weight: 400;
  }}

  .header-row {{
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
  }}

  .refresh-btn {{
    background: #21262D;
    color: #C9D1D9;
    border: 1px solid #30363D;
    border-radius: 6px;
    padding: 0.4rem 1rem;
    font-family: 'Red Hat Text', sans-serif;
    font-size: 0.8rem;
    cursor: pointer;
    transition: background 0.15s ease, border-color 0.15s ease;
    white-space: nowrap;
    margin-top: 0.25rem;
  }}

  .refresh-btn:hover {{
    background: #30363D;
    border-color: #58A6FF;
  }}
</style>
</head>
<body>

<div class="header-row">
  <div>
    <h1>RHSM Subscription Audit Report</h1>
    <p class="subtitle">Generated {now_str} &mdash; stale threshold: {stale_days} days</p>
  </div>
  <button class="refresh-btn" onclick="location.reload()">&#x21bb; Refresh</button>
</div>

<h2>Executive Summary</h2>
<div class="summary-grid">
  <a class="card-link" href="#subscriptions">
  <div class="summary-card ok">
    <span class="big-num">{total_registered}</span>
    <span class="label">Registered Systems</span>
  </div>
  </a>
  <a class="card-link" href="#subscriptions">
  <div class="summary-card">
    <span class="big-num">{total_purchased}</span>
    <span class="label">Purchased Entitlements</span>
  </div>
  </a>
  <a class="card-link" href="#subscriptions">
  <div class="summary-card {'warn' if total_delta < 0 else ''}">
    <span class="big-num">{total_delta:+d}</span>
    <span class="label">Entitlement Delta</span>
  </div>
  </a>
  <a class="card-link" href="#stale-systems">
  <div class="summary-card {'alert' if total_stale > 0 else ''}">
    <span class="big-num">{total_stale}</span>
    <span class="label">Stale Systems ({stale_days}+ days)</span>
  </div>
  </a>
  <a class="card-link" href="#untagged-systems">
  <div class="summary-card {'warn' if total_untagged > 0 else ''}">
    <span class="big-num">{total_untagged}</span>
    <span class="label">Untagged Systems</span>
  </div>
  </a>
  <a class="card-link" href="#subscriptions">
  <div class="summary-card">
    <span class="big-num">{total_consumed}</span>
    <span class="label">Consumed Entitlements</span>
  </div>
  </a>
</div>

<h2 id="subscriptions">Subscription Allocations <span class="section-count">({len(sub_rows)})</span></h2>
<table>
<thead><tr>
  <th>Subscription</th><th>SKU</th><th>Status</th>
  <th class="num">Purchased</th><th class="num">Consumed</th><th class="num">Delta</th>
</tr></thead>
<tbody>
{sub_table if sub_table else '<tr><td colspan="6" class="muted">No subscriptions found</td></tr>'}
</tbody>
</table>

<h2 id="stale-systems">Stale Systems <span class="section-count">({total_stale})</span></h2>
<p style="color:#8B949E; margin-bottom:0.75rem;">Systems with no check-in for {stale_days}+ days. Likely decommissioned but still consuming entitlements.</p>
<table>
<thead><tr>
  <th>Hostname</th><th>Last Check-in</th><th class="num">Days Since</th><th>System Purpose</th><th>Type</th>
</tr></thead>
<tbody>
{stale_table if stale_table else '<tr><td colspan="5" class="muted">No stale systems found</td></tr>'}
</tbody>
</table>

<h2 id="untagged-systems">Untagged Systems <span class="section-count">({total_untagged})</span></h2>
<p style="color:#8B949E; margin-bottom:0.75rem;">Systems with no system purpose (role/usage/SLA). These are the hardest to reconcile and often include vendor-dropped boxes (GE medical appliances, Wintel ESX hosts).</p>
<table>
<thead><tr>
  <th>Hostname</th><th>Created</th><th>Last Check-in</th><th>Type</th><th>Entitlement Status</th>
</tr></thead>
<tbody>
{untagged_table if untagged_table else '<tr><td colspan="5" class="muted">No untagged systems found</td></tr>'}
</tbody>
</table>

<h2 id="recommendations">Recommendations</h2>
<div class="recommendations">
<ol>
  {overconsumed_note}
  {'<li><strong>Deregister stale systems.</strong> ' + str(total_stale) + ' system(s) have not checked in for ' + str(stale_days) + '+ days. Each may be consuming entitlements. Review the stale list, confirm decommission status with ops, and remove via <code>subscription-manager unregister</code> or the RHSM API.</li>' if total_stale > 0 else ''}
  {'<li><strong>Tag untagged systems.</strong> ' + str(total_untagged) + ' system(s) have no system purpose set. Run <code>syspurpose set-role / set-usage / set-sla</code> on each, or set via Satellite host groups. This is critical for vendor-dropped boxes that consume VDC or other premium entitlements silently.</li>' if total_untagged > 0 else ''}
  <li><strong>Cross-reference with CMDB.</strong> Use the CSV exports alongside this report to compare against your CMDB/asset inventory. Systems in RHSM but not in the CMDB are candidates for investigation.</li>
  <li><strong>Establish system purpose policy.</strong> Require all new registrations to include system purpose metadata. Enforce via Satellite activation keys or kickstart templates.</li>
  <li><strong>Schedule recurring audits.</strong> Run this report monthly to catch drift early. Stale systems accumulate quickly in large healthcare environments with vendor-managed appliances.</li>
</ol>
</div>

<div class="footer">
  Generated by rhsm-audit.py &mdash; RHSM Subscription Audit Tool<br>
  Red Hat Subscription Management API v1 &mdash; {now_str}
</div>

</body>
</html>"""

    if report_path:
        with open(report_path, "w") as f:
            f.write(html)
        print(f"  Wrote {report_path}")
    return html


# ---------------------------------------------------------------------------
# Audit pipeline (shared by CLI and serve mode)
# ---------------------------------------------------------------------------
def run_audit(offline_token, stale_days=DEFAULT_STALE_DAYS, skip_enrich=False,
              report_path=None, output_dir=None):
    """Run the full audit pipeline. Returns HTML string."""
    datestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print(f"\n{'='*60}")
    print(f"  RHSM Subscription Audit")
    print(f"  Stale threshold: {stale_days} days")
    print(f"  Date: {datestamp}")
    print(f"{'='*60}\n")

    print("[1/5] Authenticating...")
    access_token = get_access_token(offline_token)
    print("  Authenticated successfully.\n")

    print("[2/5] Fetching systems...")
    systems = fetch_all_systems(access_token)

    if not skip_enrich and systems:
        print(f"\n[3/5] Enriching system details (system purpose, facts)...")
        systems = enrich_systems(access_token, systems)
    else:
        print(f"\n[3/5] Skipping enrichment (--skip-enrich or no systems).")
        for s in systems:
            s["_purpose_role"] = ""
            s["_purpose_usage"] = ""
            s["_purpose_sla"] = ""
            s["_is_untagged"] = True

    print(f"\n[4/5] Fetching subscriptions...")
    subs = fetch_subscriptions(access_token)

    print(f"\n[5/5] Analyzing...")
    stale, untagged = analyze_systems(systems, stale_days)
    sub_rows = analyze_subscriptions(subs)

    print(f"  Stale systems ({stale_days}+ days): {len(stale)}")
    print(f"  Untagged systems: {len(untagged)}")
    print(f"  Subscriptions: {len(sub_rows)}")

    html = generate_html(systems, stale, untagged, sub_rows, stale_days, report_path)

    if output_dir:
        all_csv = os.path.join(output_dir, f"rhsm-audit-all-systems-{datestamp}.csv")
        stale_csv = os.path.join(output_dir, f"rhsm-audit-stale-systems-{datestamp}.csv")
        untagged_csv = os.path.join(output_dir, f"rhsm-audit-untagged-systems-{datestamp}.csv")
        write_all_systems_csv(systems, all_csv)
        write_stale_csv(stale, stale_csv)
        write_untagged_csv(untagged, untagged_csv)

    return html


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="RHSM Subscription Audit — reconcile registered systems vs. purchased subscriptions"
    )
    parser.add_argument(
        "--days", type=int, default=DEFAULT_STALE_DAYS,
        help=f"Stale threshold in days (default: {DEFAULT_STALE_DAYS})"
    )
    parser.add_argument(
        "--skip-enrich", action="store_true",
        help="Skip per-system detail fetch (faster but no system purpose data)"
    )
    parser.add_argument(
        "--output-dir", type=str, default=".",
        help="Directory for output files (default: current directory)"
    )
    args = parser.parse_args()

    offline_token = os.environ.get("RHSM_OFFLINE_TOKEN", "").strip()
    if not offline_token:
        print("ERROR: RHSM_OFFLINE_TOKEN environment variable not set.", file=sys.stderr)
        print("       Generate one at https://access.redhat.com/management/api", file=sys.stderr)
        print("       Then: export RHSM_OFFLINE_TOKEN=\"your-token-here\"", file=sys.stderr)
        sys.exit(1)

    datestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    outdir = args.output_dir
    report_path = os.path.join(outdir, f"rhsm-audit-report-{datestamp}.html")

    run_audit(offline_token, args.days, args.skip_enrich, report_path, outdir)

    print(f"\nDone. Outputs in {os.path.abspath(outdir)}/")


if __name__ == "__main__":
    main()
