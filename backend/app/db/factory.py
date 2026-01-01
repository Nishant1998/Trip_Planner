from app.config import get_database_schema, require_database_url
from app.db.postgres import PostgresTripRepository


def build_trip_repository(schema: str | None = None) -> PostgresTripRepository:
    return PostgresTripRepository(
        require_database_url(),
        schema=get_database_schema() if schema is None else schema,
    )
