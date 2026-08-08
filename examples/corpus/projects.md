# Projects

Active large projects at Meridian Labs. Both are sponsored by Deniz Aydin and
reviewed at the Monday engineering staff meeting.

## Project Aurora

Project Aurora is the Pulse 2.0 effort: a ground-up rewrite of the
query-service to serve Pulse dashboards with p95 latency under 200 ms. Priya
Raman is tech lead. The project started in January 2026 after Acme Analytics
complained that large dashboard loads timed out. Aurora also folds the
retired Loom log-search use cases into Pulse cold-path queries against
Coldstore. Status: milestone 2 of 4; the new query planner is behind feature
flag `aurora.planner.v2`.

## Project Bedrock

Project Bedrock is Coldstore v2: a re-architecture of the storage tier toward
the columnar layout chosen in ADR-009, plus a proper backup/restore story.
Sam Whitfield leads it. Bedrock was prioritized after the March outage showed
that restoring a single customer's data (Acme Analytics) took six hours of
manual work. Tracepath's 13-month retention requirement is the sizing driver.
Status: design doc approved; implementation starts after Aurora milestone 2.

## On-call revamp

A smaller initiative owned by Sam Whitfield: folding the ingestion-gateway,
query-service, and alert-engine rotations into one followed-the-sun rotation
once Beacon's escalation policies (owned by Lena Fischer) are cleaned up. On
hold pending the Beacon pilot with Copperline Retail.
