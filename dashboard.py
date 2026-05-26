#!/usr/bin/env python3
"""
RHSM Audit Dashboard
====================
Live subscription audit in your browser. Every page load pulls fresh
data from the RHSM API. Click Refresh for on-demand re-query.

Usage (local):
  RHSM_OFFLINE_TOKEN=$(cat ~/.rhsm-offline-token) python3 dashboard.py

Usage (container):
  podman build -t rhsm-dashboard .
  podman run -p 8099:8099 -e RHSM_OFFLINE_TOKEN=$(cat ~/.rhsm-offline-token) rhsm-dashboard

Requires: flask, requests
"""

import importlib.util
import os
import sys
from pathlib import Path

from flask import Flask, Response

# ---------------------------------------------------------------------------
# Import audit functions from rhsm-audit.py
# ---------------------------------------------------------------------------
_here = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "rhsm_audit", _here / "rhsm-audit.py"
)
_ta = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ta)

app = Flask(__name__)

STALE_DAYS = int(os.environ.get("RHSM_STALE_DAYS", "90"))


# ---------------------------------------------------------------------------
# Dashboard JS — refresh button + loading overlay
# ---------------------------------------------------------------------------
DASHBOARD_JS = """
<style>
  #refresh-overlay {
    position: fixed; top: 0; left: 0;
    width: 100%; height: 100%;
    background: rgba(6, 9, 15, 0.93);
    display: flex; flex-direction: column;
    justify-content: center; align-items: center;
    z-index: 9999;
    font-family: 'Red Hat Display', system-ui, sans-serif;
    color: #C9D1D9;
  }
  #refresh-overlay .dash-spin {
    width: 48px; height: 48px;
    border: 4px solid #21262D;
    border-top-color: #EE0000;
    border-radius: 50%;
    animation: dspin 0.8s linear infinite;
    margin-bottom: 1.5rem;
  }
  @keyframes dspin { to { transform: rotate(360deg); } }
</style>
<script>
function doRefresh() {
  var ov = document.createElement('div');
  ov.id = 'refresh-overlay';
  ov.innerHTML = '<div class="dash-spin"></div><p>Pulling fresh data from RHSM API&hellip;</p>';
  document.body.appendChild(ov);
  fetch('/api/audit')
    .then(function(r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.text();
    })
    .then(function(h) { document.open(); document.write(h); document.close(); })
    .catch(function(e) {
      ov.innerHTML = '<p style="color:#EE0000;">Refresh failed: ' + e.message + '</p>'
        + '<button onclick="doRefresh()" style="margin-top:1rem;padding:0.5rem 1.5rem;'
        + 'background:#21262D;color:#C9D1D9;border:1px solid #30363D;border-radius:6px;'
        + 'cursor:pointer;font-family:inherit;">Retry</button>';
    });
}
</script>
"""


def inject_dashboard_js(html):
    """Rewire the refresh button and inject overlay JS."""
    html = html.replace(
        'onclick="location.reload()"',
        'onclick="doRefresh()"',
    )
    html = html.replace("</body>", DASHBOARD_JS + "</body>")
    return html


# ---------------------------------------------------------------------------
# Loading page — shown immediately, auto-fetches /api/audit
# ---------------------------------------------------------------------------
LOADING_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>RHSM Audit Dashboard</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Red+Hat+Display:wght@400;700&family=Red+Hat+Mono&display=swap');
  body {
    background: #06090F; color: #C9D1D9; margin: 0;
    font-family: 'Red Hat Display', system-ui, sans-serif;
    display: flex; justify-content: center; align-items: center;
    height: 100vh;
  }
  .center { text-align: center; }
  .spin {
    width: 56px; height: 56px;
    border: 4px solid #21262D; border-top-color: #EE0000;
    border-radius: 50%;
    animation: s 0.8s linear infinite;
    margin: 0 auto 1.5rem;
  }
  @keyframes s { to { transform: rotate(360deg); } }
  h2 { font-size: 1.4rem; margin-bottom: 0.5rem; color: #E6EDF3; }
  p { color: #8B949E; font-size: 0.9rem; margin-top: 0.25rem; }
  .step { font-family: 'Red Hat Mono', monospace; font-size: 0.8rem; color: #58A6FF; margin-top: 1rem; }
</style>
</head>
<body>
<div class="center">
  <div class="spin"></div>
  <h2>RHSM Audit Dashboard</h2>
  <p>Authenticating and pulling system data&hellip;</p>
  <p class="step" id="status"></p>
</div>
<script>
var steps = [
  'Exchanging offline token...',
  'Fetching registered systems...',
  'Enriching system details...',
  'Fetching subscriptions...',
  'Building report...'
];
var i = 0;
var el = document.getElementById('status');
var iv = setInterval(function() {
  if (i < steps.length) { el.textContent = steps[i]; i++; }
}, 8000);

fetch('/api/audit')
  .then(function(r) {
    clearInterval(iv);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.text();
  })
  .then(function(h) { document.open(); document.write(h); document.close(); })
  .catch(function(e) {
    clearInterval(iv);
    document.querySelector('.spin').style.display = 'none';
    document.querySelector('h2').textContent = 'Audit failed';
    document.querySelector('p').textContent = e.message;
  });
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return Response(LOADING_PAGE, content_type="text/html")


@app.route("/api/audit")
def audit():
    token = os.environ.get("RHSM_OFFLINE_TOKEN", "").strip()
    if not token:
        return Response(
            "<pre>RHSM_OFFLINE_TOKEN not set</pre>",
            status=500,
            content_type="text/html",
        )
    try:
        html = _ta.run_audit(token, STALE_DAYS)
        html = inject_dashboard_js(html)
        return Response(html, content_type="text/html",
                        headers={"Cache-Control": "no-store"})
    except SystemExit:
        return Response(
            "<pre>RHSM authentication failed. Check your offline token.</pre>",
            status=500,
            content_type="text/html",
        )
    except Exception as e:
        return Response(
            f"<pre>Audit error: {e}</pre>",
            status=500,
            content_type="text/html",
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    token = os.environ.get("RHSM_OFFLINE_TOKEN", "").strip()
    if not token:
        print("ERROR: RHSM_OFFLINE_TOKEN not set.", file=sys.stderr)
        print("  export RHSM_OFFLINE_TOKEN=$(cat ~/.rhsm-offline-token)", file=sys.stderr)
        sys.exit(1)

    port = int(os.environ.get("PORT", "8099"))
    print(f"\n  RHSM Audit Dashboard")
    print(f"  http://localhost:{port}")
    print(f"  Stale threshold: {STALE_DAYS} days")
    print(f"  Ctrl-C to stop\n")

    app.run(host="0.0.0.0", port=port, debug=False)
