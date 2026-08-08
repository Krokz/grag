# Products

Meridian Labs sells three observability products that share one storage
backend, Coldstore.

## Pulse

Pulse is the metrics platform and the company's flagship. Customers push
telemetry through the ingestion-gateway, and Pulse serves dashboards and
queries through the query-service. Priya Raman's team owns Pulse end to end.
Pulse is currently being rebuilt as Pulse 2.0 under Project Aurora, which
Deniz Aydin sponsors. Acme Analytics and Bluefin Health both run Pulse in
production.

## Tracepath

Tracepath is the distributed tracing product. It ingests spans through the
same ingestion-gateway as Pulse, but stores trace segments in Coldstore with
a different retention policy (13 months). Tom Okafor owns the Tracepath
pipeline. Acme Analytics adopted Tracepath in 2025 and is its largest user;
their traces were partially lost during the March outage.

## Beacon

Beacon is the alerting product. It evaluates alert rules over Pulse metrics
inside the alert-engine, which Lena Fischer owns. Beacon pages via PagerDuty
and posts to Slack. Copperline Retail is currently running a Beacon pilot.
During the March outage, Beacon was the system that first paged the on-call
rotation — the alert-engine detected the ingestion-gateway error-rate spike
four minutes after the bad deploy.

## Retired

Loom, the log-search product, was retired in 2025. Its remaining users were
migrated to Pulse plus Coldstore cold-path queries; see the glossary for the
hot path / cold path distinction.
