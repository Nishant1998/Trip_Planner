"""Check LangSmith delivery using synthetic operations and no model/API tools.

Run from backend: python scripts/check_observability.py
Reads credentials from the ignored local .env; never prints them.
"""

import asyncio
import inspect
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import ENV_FILE  # noqa: E402,F401 - load local configuration
from app.services import tracing  # noqa: E402


async def main() -> int:
    tracing.configure_tracing()
    client = tracing._client
    if client is None:
        print("Tracing is disabled or not configured.")
        return 1
    try:
        project = client.read_project(project_name=tracing._project)
        with tracing.trace_span("TripAi connection check", root=True, entry_point="check_cli"):
            root_id = tracing._current.get().id
            with tracing.trace_span("synthetic.collection"):
                collection_id = tracing._current.get().id
            with tracing.trace_span("synthetic.tool", run_type="tool"):
                tool_id = tracing._current.get().id
                tracing.trace_failure("SyntheticCheckError")
        client.flush(timeout=10)
        runs = []
        read_error = None
        for _ in range(5):
            try:
                runs = []
                for run_id in (root_id, collection_id, tool_id):
                    result = client.runs.retrieve(run_id, project_id=str(project.id),
                                                  selects=["ID", "END_TIME", "INPUTS", "OUTPUTS",
                                                           "ERROR", "PARENT_RUN_IDS", "PROJECT_ID"])
                    runs.append(await result if inspect.isawaitable(result) else result)
                if all(run.end_time for run in runs):
                    break
            except Exception as error:
                read_error = type(error).__name__
            await asyncio.sleep(1)
        if len(runs) != 3 or not all(run.end_time for run in runs):
            print(json.dumps({"status": "unverified", "read_error_type": read_error}))
            return 1
        run, *children = runs
        assert run.project_id == str(project.id)
        assert all(root_id in (child.parent_run_ids or []) for child in children)
        assert any(child.error == "SyntheticCheckError" for child in children)
        assert all(not item.inputs and not item.outputs for item in [run, *children])
        print(json.dumps({"status": "verified", "project": project.name,
                          "project_id": str(project.id), "run_id": root_id,
                          "child_spans": len(children), "private_payloads_hidden": True,
                          "model_calls": 0}))
        return 0
    except Exception as error:
        print(json.dumps({"status": "unverified", "error_type": type(error).__name__}))
        return 1
    finally:
        tracing.close_tracing()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
