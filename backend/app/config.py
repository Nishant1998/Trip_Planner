import os
import re
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BACKEND_DIR / ".env"


def load_environment() -> None:
    load_dotenv(ENV_FILE, override=False)


def require_database_url() -> str:
    load_environment()
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("Set DATABASE_URL in backend/.env before using the database.")
    if not url.startswith(("postgresql://", "postgres://")):
        raise ValueError("DATABASE_URL must be a PostgreSQL connection URL.")
    return url


def get_database_schema() -> str:
    load_environment()
    schema = os.getenv("RIYA_DATABASE_SCHEMA", "public").strip() or "public"
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema):
        raise ValueError("RIYA_DATABASE_SCHEMA must be a lowercase PostgreSQL identifier.")
    return schema
