# Customers

## Acme Analytics

Acme Analytics is the largest customer. They run Pulse for product metrics
and adopted Tracepath in 2025; they are Tracepath's heaviest user. Acme was
the customer most hurt by the March outage — four hours of telemetry lost —
and Mara Voss personally ran the apology call. Their dashboard latency
complaints in January 2026 triggered Project Aurora. Contract notes: 13-month
Tracepath retention, which drives Coldstore sizing for Project Bedrock.

## Bluefin Health

Bluefin Health runs Pulse for infrastructure monitoring (HIPAA-adjacent
workloads, so their data stays in a dedicated Coldstore namespace). During
the March outage they saw delayed dashboards but no data loss. Bluefin is
the reference account for Pulse cold-path reporting.

## Copperline Retail

Copperline Retail is running a Beacon pilot: alert rules over their Pulse
metrics, evaluated by the alert-engine, paging into their Slack. Lena Fischer
checks in weekly. If the pilot converts, Copperline wants ingestion-gateway
rate limits raised — a decision for Tom Okafor and ADR-017-compliant rollout
planning.
