# ADR 0001: Single-server containers with explicit deployment

Status: accepted for local preparation; production execution pending.
Date: 2026-10-13

## Context

TripAi is a portfolio MVP with React, FastAPI, PostgreSQL and paid AI/provider
integrations. The existing chat event bus and background planner run in one process.
The owner wants reproducible deployment commands while preparing everything locally.

## Decision

Use a Linux server with Docker Compose: Caddy serves the compiled frontend and
proxies the backend under one HTTPS origin. Run one backend worker. Keep PostgreSQL
on the existing Neon database in both local and production use.
Secrets remain runtime files outside the repository, image and release directories.
Automatic schema migration remains disabled. No new database setup is introduced.

CI builds and checks changes without production credentials. Actual deployment
automation will be connected after the hosting platform is selected. Local checks
reuse the existing settings and call only read-only endpoints. Nothing is published
during local preparation.

## Consequences

- Same-origin cookies and SSE avoid separate-site authentication complexity.
- Caddy handles certificates on the real domain; local checks use localhost HTTP.
- One backend process limits throughput and makes a restart interrupt active work.
- Future scaling needs shared queues/events; simply increasing Uvicorn workers is unsafe.
- Startup/HTTP readiness does not certify live Google sign-in, provider billing,
  itinerary quality, certificate issuance or server resources.
- Database backup/restore and first real server verification remain operator steps.

Alternatives: split static/API hosting introduces cross-origin setup; Kubernetes and
distributed workers add operating complexity that is unnecessary for this MVP.
