"""Owner-scoped LangGraph checkpoints in the existing planner-local SQLite file."""
import asyncio

from langgraph.checkpoint.base import (
    BaseCheckpointSaver, CheckpointTuple, WRITES_IDX_MAP, get_checkpoint_metadata,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from .checkpoints import CheckpointConflict
from ..models.planning import PlannerInput, PlanningResult, PlanningWorld, TripRoutePlan, ValidationIssue


class PlannerGraphSaver(BaseCheckpointSaver):
    """Persist graph cursors, channel versions and pending task writes for one run."""

    def __init__(self, store, *, run_id, owner_scope, input_hash):
        super().__init__(serde=JsonPlusSerializer(allowed_msgpack_modules=[
            PlannerInput, PlanningResult, PlanningWorld, TripRoutePlan, ValidationIssue,
        ]))
        self.store = store
        self.identity = dict(run_id=run_id, owner_scope=owner_scope, input_hash=input_hash)
        with store._connection() as connection:
            store._owned(connection, **self.identity)
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS planner_graph_checkpoints (
                    run_id TEXT NOT NULL REFERENCES planner_checkpoints(run_id),
                    namespace TEXT NOT NULL, checkpoint_id TEXT NOT NULL,
                    parent_id TEXT, value_type TEXT NOT NULL, value BLOB NOT NULL,
                    metadata_type TEXT NOT NULL, metadata BLOB NOT NULL,
                    PRIMARY KEY(run_id, namespace, checkpoint_id)
                );
                CREATE TABLE IF NOT EXISTS planner_graph_blobs (
                    run_id TEXT NOT NULL REFERENCES planner_checkpoints(run_id),
                    namespace TEXT NOT NULL, channel TEXT NOT NULL, version TEXT NOT NULL,
                    value_type TEXT NOT NULL, value BLOB NOT NULL,
                    PRIMARY KEY(run_id, namespace, channel, version)
                );
                CREATE TABLE IF NOT EXISTS planner_graph_writes (
                    run_id TEXT NOT NULL REFERENCES planner_checkpoints(run_id),
                    namespace TEXT NOT NULL, checkpoint_id TEXT NOT NULL,
                    task_id TEXT NOT NULL, idx INTEGER NOT NULL, channel TEXT NOT NULL,
                    value_type TEXT NOT NULL, value BLOB NOT NULL,
                    PRIMARY KEY(run_id, namespace, checkpoint_id, task_id, idx)
                );
            """)

    def _scope(self, config):
        values = config["configurable"]
        if values["thread_id"] != self.identity["run_id"]:
            raise CheckpointConflict("Graph checkpoint belongs to another run")
        return values.get("checkpoint_ns", "")

    def _config(self, namespace, checkpoint_id):
        return {"configurable": {"thread_id": self.identity["run_id"],
                "checkpoint_ns": namespace, "checkpoint_id": checkpoint_id}}

    def get_tuple(self, config):
        namespace = self._scope(config)
        checkpoint_id = config["configurable"].get("checkpoint_id")
        with self.store._connection() as connection:
            connection.execute("BEGIN")
            self.store._owned(connection, **self.identity)
            query = "SELECT checkpoint_id,parent_id,value_type,value,metadata_type,metadata FROM planner_graph_checkpoints WHERE run_id=? AND namespace=?"
            params = [self.identity["run_id"], namespace]
            if checkpoint_id:
                query += " AND checkpoint_id=?"
                params.append(checkpoint_id)
            row = connection.execute(query + " ORDER BY checkpoint_id DESC LIMIT 1", params).fetchone()
            if row is None:
                return None
            checkpoint_id, parent_id, kind, value, metadata_kind, metadata = row
            checkpoint = self.serde.loads_typed((kind, value))
            values = {}
            for channel, version in checkpoint["channel_versions"].items():
                blob = connection.execute(
                    "SELECT value_type,value FROM planner_graph_blobs WHERE run_id=? AND namespace=? AND channel=? AND version=?",
                    (self.identity["run_id"], namespace, channel, str(version)),
                ).fetchone()
                if blob is None:
                    raise CheckpointConflict("Graph checkpoint channel version is missing")
                if blob[0] != "empty":
                    values[channel] = self.serde.loads_typed(blob)
            checkpoint["channel_values"] = values
            writes = connection.execute(
                "SELECT task_id,channel,value_type,value FROM planner_graph_writes WHERE run_id=? AND namespace=? AND checkpoint_id=? ORDER BY task_id,idx",
                (self.identity["run_id"], namespace, checkpoint_id),
            ).fetchall()
        return CheckpointTuple(
            config=self._config(namespace, checkpoint_id), checkpoint=checkpoint,
            metadata=self.serde.loads_typed((metadata_kind, metadata)),
            parent_config=self._config(namespace, parent_id) if parent_id else None,
            pending_writes=[(task, channel, self.serde.loads_typed((kind, value)))
                            for task, channel, kind, value in writes],
        )

    def put(self, config, checkpoint, metadata, new_versions):
        namespace = self._scope(config)
        header = dict(checkpoint)
        values = header.pop("channel_values")
        encoded = self.serde.dumps_typed(header)
        encoded_metadata = self.serde.dumps_typed(get_checkpoint_metadata(config, metadata))
        blobs = [(self.identity["run_id"], namespace, channel, str(version),
                  *(self.serde.dumps_typed(values[channel]) if channel in values else ("empty", b"")))
                 for channel, version in new_versions.items()]
        with self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.store._owned(connection, **self.identity)
            connection.executemany("INSERT OR REPLACE INTO planner_graph_blobs VALUES(?,?,?,?,?,?)", blobs)
            connection.execute("INSERT OR REPLACE INTO planner_graph_checkpoints VALUES(?,?,?,?,?,?,?,?)",
                (self.identity["run_id"], namespace, checkpoint["id"],
                 config["configurable"].get("checkpoint_id"), *encoded, *encoded_metadata))
        return self._config(namespace, checkpoint["id"])

    def put_writes(self, config, writes, task_id, task_path=""):
        namespace = self._scope(config)
        rows = [(self.identity["run_id"], namespace, config["configurable"]["checkpoint_id"],
                 task_id, WRITES_IDX_MAP.get(channel, index), channel, *self.serde.dumps_typed(value))
                for index, (channel, value) in enumerate(writes)]
        with self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.store._owned(connection, **self.identity)
            for row in rows:
                action = "REPLACE" if row[4] < 0 else "IGNORE"
                connection.execute(f"INSERT OR {action} INTO planner_graph_writes VALUES(?,?,?,?,?,?,?,?)", row)

    def list(self, config, *, filter=None, before=None, limit=None):
        if config is None:
            config = {"configurable": {"thread_id": self.identity["run_id"]}}
        namespace = self._scope(config)
        if before is not None:
            self._scope(before)
        with self.store._connection() as connection:
            self.store._owned(connection, **self.identity)
            rows = connection.execute(
                "SELECT checkpoint_id FROM planner_graph_checkpoints WHERE run_id=? AND namespace=? ORDER BY checkpoint_id DESC",
                (self.identity["run_id"], namespace),
            ).fetchall()
        count = 0
        for (checkpoint_id,) in rows:
            if limit is not None and count >= limit:
                break
            requested = config["configurable"].get("checkpoint_id")
            if requested and checkpoint_id != requested:
                continue
            if before and checkpoint_id >= before["configurable"]["checkpoint_id"]:
                continue
            result = self.get_tuple(self._config(namespace, checkpoint_id))
            if filter and any(result.metadata.get(key) != value for key, value in filter.items()):
                continue
            count += 1
            yield result

    async def aget_tuple(self, config):
        return await asyncio.to_thread(self.get_tuple, config)

    async def aput(self, config, checkpoint, metadata, new_versions):
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def alist(self, config, *, filter=None, before=None, limit=None):
        results = await asyncio.to_thread(lambda: list(self.list(config, filter=filter, before=before, limit=limit)))
        for result in results:
            yield result
