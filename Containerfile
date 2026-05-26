FROM registry.access.redhat.com/ubi9/python-311:latest

LABEL name="rhsm-audit-dashboard" \
      summary="Live RHSM subscription audit dashboard" \
      description="Pulls registered systems and subscriptions from the Red Hat Subscription Management API, renders a styled audit report in the browser with click-to-drill and on-demand refresh." \
      maintainer="Jim O'Donnell <jodonnel@redhat.com>"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY trinity-audit.py dashboard.py ./

EXPOSE 8099

ENV PORT=8099 \
    RHSM_STALE_DAYS=90

CMD ["python", "dashboard.py"]
