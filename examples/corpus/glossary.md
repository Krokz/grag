# Glossary

Terms a new Meridian Labs engineer meets in the first week.

- Hot path — the synchronous request flow: ingestion-gateway -> Coldstore
  (recent data) -> query-service -> Pulse or Tracepath dashboards. Latency
  here is user-visible; Project Aurora exists to make it faster.
- Cold path — asynchronous, throttled scans over Coldstore for long-range
  reports and the retired Loom log-search use cases. Cold-path slowness is
  annoying, not an incident.
- Canary — a deploy that reaches ~5% of traffic first. Mandatory for the
  ingestion-gateway and alert-engine since ADR-017, which followed the March
  outage.
- SLO — service level objective. Pulse dashboards carry a p95 < 200 ms SLO
  (Project Aurora's target); the ingestion-gateway carries a 99.9% accept
  rate SLO, which the March outage burned through for the quarter.
- Coldstore — the columnar storage cluster (ADR-009) underneath every
  product, operated by Sam Whitfield's SRE team.
- Postmortem — a blameless written review, always owned by an SRE. Sam
  Whitfield wrote the March outage postmortem.
