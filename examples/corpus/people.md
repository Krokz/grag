# People

The engineering roster, with ownership. The rule of thumb: every service and
every product has exactly one accountable owner.

## Leadership

- Mara Voss — CEO and co-founder. Sets company strategy, talks to customers,
  and ran the Acme Analytics apology call after the March outage.
- Deniz Aydin — CTO and co-founder. Owns the technical roadmap, sponsors
  Project Aurora and Project Bedrock, and approves all ADRs.

## Engineering leads

- Priya Raman — engineering lead for Pulse. Owns the query-service and the
  Pulse dashboards. Tech lead of Project Aurora.
- Tom Okafor — engineering lead for data ingress. Owns the ingestion-gateway
  (written in Rust, see ADR-014) and the Tracepath pipeline.
- Lena Fischer — engineering lead for alerting. Owns the alert-engine and the
  Beacon product surface. First responder during the March outage.
- Sam Whitfield — SRE lead. Owns Coldstore operations, backups, and capacity
  planning. Wrote the March outage postmortem and now leads Project Bedrock.

## Team map

Priya, Tom, and Lena report to Deniz Aydin. Sam Whitfield's SRE team is
embedded with all three leads. On-call rotations are staffed by the owning
team: ingestion-gateway pages go to Tom Okafor's rotation, alert-engine pages
to Lena Fischer's, and Coldstore or query-service pages to Sam Whitfield's
and Priya Raman's rotations respectively.
