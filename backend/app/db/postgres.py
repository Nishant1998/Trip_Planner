from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
import re

import psycopg
from psycopg import sql
from psycopg.rows import dict_row


class PostgresTripRepository:
    def __init__(self, database_url: str, *, schema: str = "public") -> None:
        if not database_url.strip():
            raise ValueError("DATABASE_URL is required.")
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema):
            raise ValueError("Invalid PostgreSQL schema name.")
        self.database_url = database_url
        self.schema = schema

    @contextmanager
    def _connect(self) -> Iterator[psycopg.Connection]:
        with psycopg.connect(
            self.database_url, row_factory=dict_row, connect_timeout=5
        ) as connection:
            connection.execute("SET statement_timeout TO '10s'")
            connection.execute(
                sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema))
            )
            yield connection

    def init_db(self) -> None:
        """Create the initial schema explicitly, in a single transaction."""
        migration = Path(__file__).parent / "migrations" / "001_init.sql"
        with self._connect() as connection:
            connection.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                    sql.Identifier(self.schema)
                )
            )
            connection.execute(migration.read_text(encoding="utf-8"))

    def check_connection(self) -> None:
        """Verify connectivity and initial tables without creating anything."""
        with self._connect() as connection:
            connection.execute("SELECT 1")
            for table in ("users", "trips"):
                row = connection.execute(
                    "SELECT to_regclass(%s) AS relation",
                    (f"{self.schema}.{table}",),
                ).fetchone()
                if row["relation"] is None:
                    raise RuntimeError(
                        f"Missing {table} table in schema {self.schema}. "
                        "Run uv run python -m app.db init."
                    )
