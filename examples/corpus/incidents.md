# Incidents

## The March outage (2026-03-14)

The March outage is the reference incident for Meridian Labs; new hires walk
through it during onboarding.

### What happened

At 14:07 UTC a configuration deploy to the ingestion-gateway flipped the
batch-compression flag globally. The new code path silently dropped roughly
30% of incoming batches. Pulse and Tracepath went blind for about four
hours; Beacon's alert-engine (Lena Fischer) detected the error-rate spike
and paged at 14:11, but the runbook at the time did not cover config-only
deploys, so rollback took until 18:02.

### Root cause

A bad configuration deploy to the ingestion-gateway, rolled out globally
with no canary. Tom Okafor's team had no staging environment that exercised
the compression flag with production-shaped traffic.

### Impact

Acme Analytics lost four hours of telemetry and a slice of Tracepath spans;
Bluefin Health saw delayed Pulse dashboards. Mara Voss ran the apology call
with Acme Analytics.

### Follow-ups

Sam Whitfield wrote the postmortem. The action items became ADR-017
(mandatory canary deploys) and accelerated Project Bedrock, because
restoring Acme Analytics' data from Coldstore took six manual hours.
