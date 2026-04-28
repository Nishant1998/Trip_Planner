# Deployment preparation

The app is packaged as two containers: **web** (the compiled React site and Caddy)
and **backend** (FastAPI). They can run together on one Linux server. The existing
Neon database and provider APIs stay external and keep their current configuration.

## Local build and verification

Start Docker Desktop in Linux-container mode. From the repository root, copy
`deploy/.env.example` to `deploy/.env` if no configured file exists. Set
`GOOGLE_OAUTH_CLIENT_ID` to your existing public client ID. `BACKEND_ENV_FILE` defaults
to `backend/.env`, so there is no need to duplicate your keys or database URL.

```sh
docker compose --env-file deploy/.env -p tripai-local config --quiet
docker compose --env-file deploy/.env -p tripai-local up -d --build --wait --wait-timeout 180
python deploy/smoke.py --url http://localhost:8080 --ready
```

If you changed `HTTP_PORT`, use that port in the smoke URL and `PUBLIC_ORIGIN`.
`config --quiet` validates without printing secret values; avoid plain `config`,
which expands the runtime environment. The smoke check does not sign in, create
trips, call a model, change database rows or refresh paid provider data.

## Files to publish later

Publish the application code, Dockerfiles, Compose files, lockfiles, templates,
CI workflow and documentation. Keep `.env`, private logs, database dumps and
generated private conversations outside Git. Review repository history and rotate
keys that were ever disclosed before a public push.

The GitHub **CI** workflow checks `main` and `dev` pushes and pull requests. It
builds both images and checks backend liveness without connecting to Neon. It has
no production credentials and cannot deploy. Select the host first, then connect
its deployment mechanism or a small SSH workflow to successful CI.

## Future server setup

Use a Docker-capable Linux host with outbound HTTPS access to Neon and the APIs.
Install Docker Engine/Compose, Git and Python 3.11+. Clone the repository into an
application directory such as `/opt/tripai`.

Keep the existing backend settings in an ignored runtime file on the server;
`deploy/backend.env.example` is a blank reference, not a replacement database.
Preserve `DATABASE_URL`, `RIYA_DATABASE_SCHEMA` and your configured provider keys.
Leave `RIYA_AUTO_MIGRATE=false`.

Create `deploy/.env` on that server, for example:

```dotenv
BACKEND_ENV_FILE=/opt/tripai-secrets/backend.env
GOOGLE_OAUTH_CLIENT_ID=YOUR_EXISTING_PUBLIC_CLIENT_ID
PUBLIC_ORIGIN=https://tripai.your-domain.example
SITE_ADDRESS=tripai.your-domain.example
IMAGE_TAG=release
```

Protect the backend runtime file with owner-only access. Point DNS at the server,
allow inbound ports 80/443, and add the HTTPS website origin to your Google OAuth
client's authorized JavaScript origins. The frontend client ID must match the backend.
Use the domain without a scheme for `SITE_ADDRESS`; Caddy requests and renews HTTPS
certificates. The backend's port 8000 is not published.

When you later choose to deploy, the command is:

```sh
docker compose --env-file deploy/.env -p tripai -f compose.yaml -f compose.production.yaml up -d --build --wait --wait-timeout 180
python3 deploy/smoke.py --url https://tripai.your-domain.example --ready
```

The production override publishes ports 80/443. Do not run it on this local
preparation session. Replace the example domain with the domain you actually own.

## First real server acceptance

After container health checks pass, verify Google login, a small complete trip,
live planning updates, saved-trip ownership, demo allowances and LangSmith delivery.
Confirm prices and uncertain itinerary details remain labelled. Test a restart
when no planning job is active and confirm stored trips persist in Neon.

Local checks cannot prove domain DNS, certificate issuance, real OAuth acceptance,
server capacity or provider billing. Those checks belong to the chosen server.

## Vercel

Vercel can serve the React frontend and supports FastAPI through Functions. This
backend currently runs background tasks and SSE events inside a persistent process;
moving it to Functions requires a lifecycle/job-coordination review. To preserve
the current planner, run both containers on one Docker host, or keep only the
frontend on Vercel and the backend on a persistent service. The split option also
needs correct proxy/cookie and Google-origin settings, so it is not the default here.

References: [Docker Compose production](https://docs.docker.com/compose/how-tos/production/),
[Caddy reverse proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy),
[Google login setup](https://developers.google.com/identity/gsi/web/guides/get-google-api-clientid),
[Vercel FastAPI](https://vercel.com/docs/frameworks/backend/fastapi).
