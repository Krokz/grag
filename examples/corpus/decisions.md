# Architecture Decision Records

ADRs are approved by Deniz Aydin. Only the records still shaping daily work
are summarized here.

## ADR-009: Coldstore goes columnar

Coldstore stores metrics and trace segments in a columnar layout instead of
row storage. Rationale: Pulse dashboards and Tracepath queries scan a few
columns over huge time ranges. This decision is the foundation of Project
Bedrock (Coldstore v2), led by Sam Whitfield.

## ADR-014: ingestion-gateway in Rust

The ingestion-gateway was rewritten from Python to Rust for predictable tail
latency under burst load. Tom Okafor drove the rewrite. Consequence: all
ingest-path changes need a Rust reviewer from Tom Okafor's team.

## ADR-017: canary deploys are mandatory

Adopted two weeks after the March outage. The March outage was caused by a
global, non-canaried configuration deploy to the ingestion-gateway. ADR-017
requires that the ingestion-gateway and the alert-engine deploy to a 5%
canary cohort for at least 30 minutes with automatic rollback on error-rate
regression, before any global rollout. Sam Whitfield owns the canary
tooling; Lena Fischer wired the alert-engine health checks that drive the
automatic rollback. The deploy runbook in runbooks.md encodes this.
