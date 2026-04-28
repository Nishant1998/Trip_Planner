# Local preparation verification

Checked on 2026-10-13. This records local preparation, not a real server launch.

## Passed

- Both Docker images built successfully from locked application dependencies.
- Both app containers became healthy, with only the web port bound to localhost.
- The existing Neon configuration passed the read-only readiness check.
- Landing page, three sample routes and production assets passed HTTP checks.
- The browser rendered the public landing page and Paris itinerary without overflow or console errors.
- A synthetic SSE event passed through the same Caddy configuration in 0.018 seconds,
  before a delayed second frame; API streaming is not compressed or buffered.
- Anonymous access to private trips returned 401.
- Four production guards rejected test login, development auth, missing Google client ID and wildcard CORS.
- 222 external backend regressions passed inside Linux. Only Windows path references
  were adapted in a separate external test copy; original assertions and tests remain intact.
- Eight external public-sample rendering checks passed.
- Ruff, frontend lint, TypeScript/build, Caddy validation and GitHub workflow lint passed.
- Python and npm dependency audits reported no known vulnerabilities at check time.
- Images contain no backend `.env`, private generated sample runs or database setup helper.

No model calls, database writes, schema changes, GitHub push or remote deployment
were performed for these checks. Neon and existing provider settings remain unchanged.

## Still checked at the actual host

DNS, HTTPS certificate issuance, Google login for the final domain, server resources,
and one complete live trip with provider responses. CI currently builds/checks only;
actual deployment automation is connected after the host is selected. Liveness and
readiness do not guarantee live provider availability or itinerary accuracy.
