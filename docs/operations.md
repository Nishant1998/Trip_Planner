# Operating TripAi

Use the same Compose options and project name that started the app. Commands below
assume the future server configuration; locally use `-p tripai-local` and omit
`-f compose.yaml -f compose.production.yaml`.

## Status and logs

```sh
docker compose --env-file deploy/.env -p tripai -f compose.yaml -f compose.production.yaml ps
docker compose --env-file deploy/.env -p tripai -f compose.yaml -f compose.production.yaml logs --tail=100 backend
docker compose --env-file deploy/.env -p tripai -f compose.yaml -f compose.production.yaml logs --tail=100 web
```

Container console logs are JSON where the app emits structured events, with bounded
Docker rotation. Use request IDs, trip IDs and job IDs to follow an operation. Never
share raw runtime environment output or logs containing personal data publicly.
LangSmith records explicit operational metadata when enabled; inputs, outputs and
profiles are hidden. The synthetic tracing script can check delivery without AI calls.

`/health/live` means the process responds. `/health/ready` checks required provider
configuration and Neon connectivity; it does not call the providers or certify a plan.

## Update and rollback

Keep the current deployed commit SHA before an update. Update only when no chat
turn or planning job is active: a restart interrupts process-local work.

```sh
git rev-parse HEAD
git fetch origin
git switch --detach <COMMIT_SHA_TO_RUN>
docker compose --env-file deploy/.env -p tripai -f compose.yaml -f compose.production.yaml up -d --build --wait --wait-timeout 180
python3 deploy/smoke.py --url https://YOUR_DOMAIN --ready
```

For rollback, use the same commands with the previous SHA. Keep the runtime files
outside Git, so switching source versions preserves the settings. Do not reset
dirty checkouts, delete Neon data or run migrations as part of an app rollback.
If a job was interrupted, inspect its recorded status and use an explicit retry.

## Database

Neon remains the existing source of truth. This Docker setup has no database service
and makes no schema change. Maintain a restore point/backup using your Neon account's
available backup features before a real release; confirm its retention and restoration
process in the provider dashboard. Any future migration needs a separately reviewed
plan and backup. `RIYA_AUTO_MIGRATE` stays false.

## Common problems

- **Google login fails:** check the actual HTTPS origin and the shared public client ID.
- **Readiness is 503:** check the existing database connection/schema and required key names.
- **Planning is disabled:** check `PLANNING_ENABLED` in the ignored backend runtime file.
- **No live updates:** check one backend worker, the `/api/.../stream` route and proxy logs.
- **A dependency or build check fails:** repair the failing check; do not bypass CI gates.
- **LangSmith is empty:** check `TRIPAI_TRACING_ENABLED`, project, region and the rotated key.
- **Old frontend configuration:** Vite settings are built into the bundle; rebuild the web image.
