# Local development

Use Python 3.11 and Node.js 22. Keep development configuration in the ignored
`backend/.env` and `frontend/.env` files. Docker reads the same backend file at
runtime; its build context excludes this file so keys never enter the image.

## Backend

From `backend/` on Windows PowerShell:

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.lock
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# Fill a new .env locally, or preserve the existing configuration.
.venv/Scripts/python.exe -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

On Linux/macOS use `.venv/bin/python` and `cp` instead of the Windows equivalents.
Run the copy command only when `.env` does not already exist.

Use `DATABASE_URL` and `RIYA_DATABASE_SCHEMA` for the selected development database.
Reuse the existing Neon schema. Keep `RIYA_AUTO_MIGRATE=false`; container startup
does not create, alter or migrate any table.

Real planning requires OpenAI, Places/Maps and route-provider configuration.
`PLANNING_ENABLED=true` enables planning after confirmation. Check `/health/ready`;
it validates configuration and database connectivity without calling an LLM.
Booking-provider settings are optional and may yield unavailable options when absent.

## Frontend

From `frontend/`:

```sh
npm ci
# Copy .env.example to .env if no configured .env exists.
npm run dev
```

Set `VITE_GOOGLE_CLIENT_ID` to the same public client ID as the backend's
`GOOGLE_OAUTH_CLIENT_ID`. Set `VITE_API_BASE=http://127.0.0.1:8000` for native local
development. Configure the localhost origins in Google Cloud.

Open http://localhost:5173. The Docker frontend uses an empty API base and routes
requests through Caddy to the backend on the same website origin.

## Local container verification

Follow the README commands. Inspect with:

```sh
docker compose --env-file deploy/.env -p tripai-local ps
docker compose --env-file deploy/.env -p tripai-local logs --tail=100 backend
docker compose --env-file deploy/.env -p tripai-local down
```

`down` stops only these app containers. Neon remains external and untouched. The
named Docker volumes contain Caddy state, not the application database.

## LangSmith

Use `TRIPAI_TRACING_ENABLED=true`, `LANGSMITH_PROJECT=TripAi` and a locally stored
`LANGSMITH_API_KEY`. Select the endpoint for your LangSmith account's region.
Automatic LangChain/LangGraph tracing stays off; TripAi sends explicit operational
metadata and hides raw inputs/outputs. Run `scripts/check_observability.py` to verify
delivery without making an OpenAI call. Costs shown by LangSmith are estimates when
it recognizes the configured model; provider billing remains authoritative.
