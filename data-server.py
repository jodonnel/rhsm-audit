#!/usr/bin/env python3
"""
Lightweight HTTP server that runs the RHSM audit and serves results
as JSON for Grafana's Infinity datasource.

Endpoints:
  GET /health          → {"status": "ok", "last_refresh": "..."}
  GET /subscriptions   → [{name, sku, status, purchased, consumed, delta}, ...]
  GET /systems         → [{hostname, os, kernel, type, last_checkin, purpose, ...}, ...]
  GET /stale           → [{hostname, last_checkin, days_since, purpose, type}, ...]
  GET /untagged        → [{hostname, created, last_checkin, type, entitlement_status}, ...]
  GET /summary         → {registered, purchased, consumed, delta, stale, untagged}
  POST /refresh        → re-run the audit now

Runs on port 8080 by default. Set DATA_SERVER_PORT to override.
"""

import importlib.util
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

_here = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("rhsm_audit", _here / "rhsm-audit.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

DATA = {
    "systems": [],
    "subscriptions": [],
    "stale": [],
    "untagged": [],
    "summary": {},
    "last_refresh": None,
    "error": None,
}
LOCK = threading.Lock()


def _fetch_insights_uuids(access_token):
    """Query Insights Host Inventory API. Returns {subscription_manager_id: insights_id}."""
    import requests
    mapping = {}
    try:
        page = 1
        per_page = 100
        while True:
            resp = requests.get(
                "https://console.redhat.com/api/inventory/v1/hosts",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
                params={"per_page": per_page, "page": page},
                timeout=60,
            )
            if resp.status_code != 200:
                print(f"[data-server] Insights inventory API returned {resp.status_code}", flush=True)
                break
            data = resp.json()
            hosts = data.get("results", [])
            for h in hosts:
                sm_id = h.get("subscription_manager_id", "")
                if sm_id:
                    mapping[sm_id] = h["id"]
            if len(hosts) < per_page:
                break
            page += 1
            time.sleep(0.25)
        print(f"[data-server] Insights inventory: mapped {len(mapping)} hosts", flush=True)
    except Exception as e:
        print(f"[data-server] Insights inventory fetch failed: {e}", flush=True)
    return mapping


def run_audit():
    token = os.environ.get("RHSM_OFFLINE_TOKEN", "").strip()
    if not token:
        DATA["error"] = "RHSM_OFFLINE_TOKEN not set"
        return

    try:
        access_token = _mod.get_access_token(token)
        systems = _mod.fetch_all_systems(access_token)
        systems = _mod.enrich_systems(access_token, systems)
        subs = _mod.fetch_subscriptions(access_token)
        stale, untagged = _mod.analyze_systems(systems, 90)
        sub_rows = _mod.analyze_subscriptions(subs)

        insights_map = _fetch_insights_uuids(access_token)

        def fmt_date(d):
            dt = _mod.parse_date(d)
            return dt.strftime("%Y-%m-%d") if dt else "never"

        sys_out = []
        for s in systems:
            purpose = " / ".join(filter(None, [
                s.get("_purpose_role", ""),
                s.get("_purpose_usage", ""),
                s.get("_purpose_sla", ""),
            ]))
            sm_uuid = s.get("uuid", "")
            insights_id = insights_map.get(sm_uuid, "")
            hostname = s.get("hostname") or s.get("name", "")
            if insights_id:
                console_url = f"https://console.redhat.com/insights/inventory/{insights_id}"
            else:
                console_url = f"https://console.redhat.com/insights/inventory?hostname_or_id={hostname}"
            sys_out.append({
                "hostname": s.get("hostname") or s.get("name", ""),
                "uuid": sm_uuid,
                "console_url": console_url,
                "insights_registered": bool(insights_id),
                "type": s.get("type", ""),
                "os": "",
                "last_checkin": fmt_date(s.get("lastCheckin")),
                "created": fmt_date(s.get("createdDate")),
                "entitlement_status": s.get("entitlementStatus", ""),
                "entitlement_count": s.get("entitlementCount", 0),
                "purpose": purpose or "none",
                "is_untagged": s.get("_is_untagged", False),
            })

        stale_out = []
        for s in stale:
            purpose = " / ".join(filter(None, [
                s.get("_purpose_role", ""),
                s.get("_purpose_usage", ""),
                s.get("_purpose_sla", ""),
            ]))
            stale_out.append({
                "hostname": s.get("hostname") or s.get("name", ""),
                "last_checkin": fmt_date(s.get("lastCheckin")),
                "days_since": s.get("_days_since_checkin", "never"),
                "purpose": purpose or "none",
                "type": s.get("type", ""),
            })

        untagged_out = []
        for s in untagged:
            untagged_out.append({
                "hostname": s.get("hostname") or s.get("name", ""),
                "created": fmt_date(s.get("createdDate")),
                "last_checkin": fmt_date(s.get("lastCheckin")),
                "type": s.get("type", ""),
                "entitlement_status": s.get("entitlementStatus", ""),
            })

        total_purchased = sum(r["purchased"] for r in sub_rows if r["purchased"] > 0)
        total_consumed_api = sum(r["consumed"] for r in sub_rows)

        with LOCK:
            DATA["systems"] = sys_out
            DATA["subscriptions"] = sub_rows
            DATA["stale"] = stale_out
            DATA["untagged"] = untagged_out
            DATA["summary"] = {
                "registered": len(systems),
                "purchased": total_purchased,
                "consumed_api": total_consumed_api,
                "consuming": len(systems),
                "delta": total_purchased - len(systems),
                "stale": len(stale),
                "untagged": len(untagged),
            }
            DATA["last_refresh"] = datetime.now(timezone.utc).isoformat()
            DATA["error"] = None

        print(f"[data-server] Audit complete: {len(systems)} systems, {len(sub_rows)} subscriptions", flush=True)

    except Exception as e:
        DATA["error"] = str(e)
        print(f"[data-server] Audit failed: {e}", file=sys.stderr, flush=True)


def refresh_loop(interval_minutes=60):
    while True:
        time.sleep(interval_minutes * 60)
        print(f"[data-server] Scheduled refresh...", flush=True)
        run_audit()


class Handler(BaseHTTPRequestHandler):
    def _parse_path(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        return parsed.path.rstrip("/"), parse_qs(parsed.query)

    def do_GET(self):
        path, params = self._parse_path()
        filt = params.get("filter", ["all"])[0]
        with LOCK:
            if path == "/health":
                body = {"status": "ok", "last_refresh": DATA["last_refresh"], "error": DATA["error"]}
            elif path == "/subscriptions":
                body = DATA["subscriptions"]
            elif path == "/systems":
                systems = DATA["systems"]
                if filt == "stale":
                    stale_hosts = {s["hostname"] for s in DATA["stale"]}
                    systems = [s for s in systems if s["hostname"] in stale_hosts]
                elif filt == "untagged":
                    systems = [s for s in systems if s.get("is_untagged")]
                elif filt == "tagged":
                    systems = [s for s in systems if not s.get("is_untagged")]
                body = systems
            elif path == "/stale":
                body = DATA["stale"]
            elif path == "/untagged":
                body = DATA["untagged"]
            elif path == "/summary":
                body = DATA["summary"]
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'{"error": "not found"}')
                return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(body, indent=2).encode())

    def do_POST(self):
        if self.path.rstrip("/") == "/refresh":
            threading.Thread(target=run_audit, daemon=True).start()
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "refresh started"}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def main():
    port = int(os.environ.get("DATA_SERVER_PORT", "8080"))
    refresh_min = int(os.environ.get("REFRESH_INTERVAL_MINUTES", "60"))

    print(f"[data-server] Running initial audit...", flush=True)
    run_audit()

    t = threading.Thread(target=refresh_loop, args=(refresh_min,), daemon=True)
    t.start()

    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"[data-server] Listening on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
