"""Planner-local SQLite checkpoints and immutable terminal artifacts."""
from contextlib import contextmanager
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from copy import deepcopy

from .contracts import fingerprint


class CheckpointConflict(RuntimeError):
    pass


class PlannerCheckpointStore:
    """An explicit file target; callers supply authenticated owner/run identity."""

    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS planner_checkpoints (
                    run_id TEXT PRIMARY KEY,
                    owner_scope TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS planner_artifacts (
                    run_id TEXT PRIMARY KEY REFERENCES planner_checkpoints(run_id),
                    checkpoint_revision INTEGER NOT NULL,
                    artifact_hash TEXT NOT NULL,
                    artifact_json TEXT NOT NULL
                );
            """)

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _encode(value):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)

    @staticmethod
    def _owned(connection, run_id, owner_scope, input_hash):
        row = connection.execute(
            "SELECT revision,state_json FROM planner_checkpoints WHERE run_id=? AND owner_scope=? AND input_hash=?",
            (run_id, owner_scope, input_hash),
        ).fetchone()
        if row is None:
            raise CheckpointConflict("Checkpoint absent or identity mismatch")
        return row

    def create(self, *, run_id: str, owner_scope: str, input_hash: str, state: dict):
        encoded = self._encode(state)
        with self._connection() as connection:
            try:
                connection.execute("INSERT INTO planner_checkpoints VALUES(?,?,?,?,?,?)",
                    (run_id, owner_scope, input_hash, 0, encoded, datetime.now(UTC).isoformat()))
            except sqlite3.IntegrityError as exc:
                raise CheckpointConflict("Run already exists; resume instead") from exc
        return 0

    def load(self, *, run_id: str, owner_scope: str, input_hash: str):
        with self._connection() as connection:
            revision, encoded = self._owned(connection, run_id, owner_scope, input_hash)
        return revision, json.loads(encoded)

    def save(self, *, run_id: str, owner_scope: str, input_hash: str,
             expected_revision: int, state: dict):
        encoded = self._encode(state)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._owned(connection, run_id, owner_scope, input_hash)
            if connection.execute("SELECT 1 FROM planner_artifacts WHERE run_id=?", (run_id,)).fetchone():
                raise CheckpointConflict("Finalized run is immutable")
            updated = connection.execute(
                "UPDATE planner_checkpoints SET revision=revision+1,state_json=?,updated_at=? "
                "WHERE run_id=? AND owner_scope=? AND input_hash=? AND revision=?",
                (encoded, datetime.now(UTC).isoformat(), run_id, owner_scope, input_hash, expected_revision),
            )
            if updated.rowcount != 1:
                raise CheckpointConflict("Stale checkpoint revision")
        return expected_revision + 1

    def finalize(self, *, run_id: str, owner_scope: str, input_hash: str,
                 expected_revision: int, artifact: dict):
        encoded, digest = self._encode(artifact), fingerprint(artifact)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            revision, _ = self._owned(connection, run_id, owner_scope, input_hash)
            if revision != expected_revision:
                raise CheckpointConflict("Cannot publish an unchecked checkpoint revision")
            existing = connection.execute(
                "SELECT artifact_hash FROM planner_artifacts WHERE run_id=?", (run_id,),
            ).fetchone()
            if existing:
                if existing[0] != digest:
                    raise CheckpointConflict("Terminal artifact cannot change")
                return
            connection.execute("INSERT INTO planner_artifacts VALUES(?,?,?,?)",
                               (run_id, revision, digest, encoded))

    def load_artifact(self, *, run_id: str, owner_scope: str, input_hash: str):
        with self._connection() as connection:
            self._owned(connection, run_id, owner_scope, input_hash)
            row = connection.execute("SELECT artifact_json FROM planner_artifacts WHERE run_id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None


class CheckpointSession:
    """Revision-bound callback that retains full state on utility-only saves."""

    def __init__(self, store: PlannerCheckpointStore, *, run_id: str,
                 owner_scope: str, input_hash: str):
        self.store = store
        self.identity = dict(run_id=run_id, owner_scope=owner_scope, input_hash=input_hash)
        self.revision, self.state = store.load(**self.identity)

    async def save(self, changes: dict):
        candidate = {**self.state, **deepcopy(changes)}
        revision = self.store.save(**self.identity, expected_revision=self.revision, state=candidate)
        self.state, self.revision = candidate, revision
