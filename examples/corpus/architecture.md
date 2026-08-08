# Architecture

One data plane, three products. Everything customers send us flows through
the same pipe.

## Data flow

1. Customer agents POST metrics and spans to the ingestion-gateway. The
   ingestion-gateway is written in Rust (ADR-014), is owned by Tom Okafor,
   and enforces per-customer rate limits and auth.
2. The ingestion-gateway writes accepted batches to Coldstore, our storage
   cluster. Coldstore is columnar (ADR-009) and is operated by Sam
   Whitfield's SRE team.
3. The query-service reads from Coldstore and serves the Pulse and Tracepath
   APIs and dashboards. Priya Raman owns the query-service.
4. The alert-engine (Beacon's backend, owned by Lena Fischer) evaluates
   alert rules over Pulse metrics every 30 seconds and pages via PagerDuty.

## Hot path and cold path

The hot path is ingestion-gateway -> Coldstore (recent data) ->
query-service. Cold path queries (long-range reports, the retired Loom log
use cases) scan Coldstore directly and are throttled; see the glossary.

## Blast radius notes

The ingestion-gateway is the single point of failure for all ingest: when it
misleads, Pulse, Tracepath, and Beacon all go blind at once. That is exactly
what happened in the March outage, and it is why ADR-017 now requires canary
deploys for the ingestion-gateway and the alert-engine before any global
rollout.
