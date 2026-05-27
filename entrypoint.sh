#!/bin/bash
# Start data server in background, then launch Grafana
echo "[entrypoint] Starting RHSM data server on :8080..."
python3 /opt/rhsm-audit/data-server.py &
DATA_PID=$!

# Wait for data server to be ready (audit enrichment can take 2+ minutes)
echo "[entrypoint] Waiting for initial audit to complete..."
for i in $(seq 1 180); do
    if curl -sf http://localhost:8080/health >/dev/null 2>&1; then
        echo "[entrypoint] Data server ready."
        break
    fi
    sleep 2
done

echo "[entrypoint] Starting Grafana on :3000..."
exec /run.sh
