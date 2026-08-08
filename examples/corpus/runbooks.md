# Runbooks

## Deploying the ingestion-gateway

Owner: Tom Okafor. The ingestion-gateway deploys must follow ADR-017: deploy
to the 5% canary cohort, bake for 30 minutes, and let the alert-engine health
checks decide. Automatic rollback triggers on any error-rate regression.
Never do a global config push — that is exactly what caused the March
outage. If the canary fails, page Tom Okafor's rotation and freeze deploys.

## Alerting on-call (alert-engine / Beacon)

Owner: Lena Fischer. Beacon alert rules are evaluated by the alert-engine
every 30 seconds over Pulse metrics. If the alert-engine falls behind, check
Coldstore read latency first (Sam Whitfield's dashboard), then the
query-service. Escalation: Lena Fischer, then Deniz Aydin.

## Coldstore restore

Owner: Sam Whitfield. Restores are manual until Project Bedrock lands.
Restoring one customer's namespace (the Acme Analytics restore after the
March outage) takes about six hours; plan customer communication through
Mara Voss's office before starting.

## Query-service deploys

Owner: Priya Raman. The query-service serves both Pulse and Tracepath, so
deploys are coordinated in the #project-aurora channel while the Aurora
planner flag (`aurora.planner.v2`) is rolling out.
