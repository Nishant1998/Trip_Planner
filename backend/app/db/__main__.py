import argparse
import sys

import psycopg

from app.db.factory import build_trip_repository


def main() -> int:
    parser = argparse.ArgumentParser(description="Set up or check the trip database.")
    parser.add_argument("command", choices=("init", "check"))
    args = parser.parse_args()
    try:
        repository = build_trip_repository()
        if args.command == "init":
            repository.init_db()
            print(f"Initial tables are ready in schema {repository.schema}.")
        else:
            repository.check_connection()
            print(f"Database and initial tables are reachable in {repository.schema}.")
    except psycopg.Error:
        print(
            "Database operation failed. Check the server, DATABASE_URL, "
            "and the user's schema permissions.",
            file=sys.stderr,
        )
        return 1
    except (RuntimeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
