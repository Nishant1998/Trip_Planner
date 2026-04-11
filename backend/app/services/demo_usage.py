"""Account allowances stored in users.preferences, outside deletable trips."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone

from fastapi import HTTPException
from psycopg.types.json import Json

from app.config import (
    RATE_LIMIT_MESSAGES_PER_DAY,
    RATE_LIMIT_MESSAGES_PER_MINUTE,
    RATE_LIMIT_TRIPS_PER_MONTH,
)

IST = timezone(timedelta(hours=5, minutes=30), "IST")


def usage_summary(preferences, role="user", *, now=None):
    now = (now or datetime.now(UTC)).astimezone(IST)
    full = role == "admin" or preferences.get("demo_access") == "full"
    usage = preferences.get("demo_usage") or {}
    month = now.strftime("%Y-%m")
    day = now.date().isoformat()
    next_month = (now.replace(day=28) + timedelta(days=4)).replace(
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    def allowance(used, limit, reset):
        limit = None if full or limit <= 0 else limit
        return {
            "used": used,
            "limit": limit,
            "remaining": max(0, limit - used) if limit is not None else None,
            "resets_at": reset.isoformat(),
        }

    return {
        "access": "full" if full else "demo",
        "timezone": "Asia/Kolkata",
        "planning": allowance(
            usage.get("plans_used", 0) if usage.get("plans_month") == month else 0,
            RATE_LIMIT_TRIPS_PER_MONTH,
            next_month,
        ),
        "chat": allowance(
            usage.get("chat_used", 0) if usage.get("chat_day") == day else 0,
            RATE_LIMIT_MESSAGES_PER_DAY,
            (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            ),
        ),
    }


def consume(preferences, kind, role="user", *, now=None):
    now = (now or datetime.now(UTC)).astimezone(IST)
    result = deepcopy(preferences)
    summary = usage_summary(result, role, now=now)
    if summary["access"] == "full":
        return result
    usage = result.setdefault("demo_usage", {})
    key = "planning" if kind == "plan" else "chat"
    allowance = summary[key]
    if allowance["remaining"] == 0:
        label = "Monthly planning" if kind == "plan" else "Daily chat"
        raise HTTPException(
            429,
            f"{label} limit reached. Resets at {datetime.fromisoformat(allowance['resets_at']).strftime('%d %b %Y, %I:%M %p')} IST.",
        )
    if kind == "plan":
        if usage.get("plans_month") != now.strftime("%Y-%m"):
            usage["charged_runs"] = []
            usage["refunded_runs"] = []
        usage.update(
            plans_month=now.strftime("%Y-%m"), plans_used=allowance["used"] + 1
        )
    else:
        recent = [t for t in usage.get("chat_minute", []) if now.timestamp() - t < 60]
        if (
            RATE_LIMIT_MESSAGES_PER_MINUTE > 0
            and len(recent) >= RATE_LIMIT_MESSAGES_PER_MINUTE
        ):
            raise HTTPException(
                429, "Please wait a minute before sending another message."
            )
        usage.update(
            chat_day=now.date().isoformat(),
            chat_used=allowance["used"] + 1,
            chat_minute=[*recent, now.timestamp()],
        )
    return result


def lock_account(conn, user_id):
    row = conn.execute(
        "SELECT id, role, preferences FROM users WHERE id=%s FOR UPDATE",
        (user_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Account not found.")
    return row


def charge_account(conn, account, kind, run_id=None):
    preferences = consume(account["preferences"] or {}, kind, account["role"])
    if run_id and usage_summary(preferences, account["role"])["access"] == "demo":
        usage = preferences["demo_usage"]
        usage["charged_runs"] = [*usage.get("charged_runs", [])[-19:], run_id]
    conn.execute(
        "UPDATE users SET preferences=%s WHERE id=%s",
        (Json(preferences), account["id"]),
    )


def reserve_planning(conn, account, trip_id, run_id):
    active = conn.execute(
        """SELECT j.id FROM planning_jobs j JOIN trips t ON t.id=j.trip_id
           WHERE t.owner_id=%s AND j.trip_id<>%s AND j.status IN ('queued','running')
           AND j.updated_at::timestamptz > NOW() - INTERVAL '6 minutes' LIMIT 1""",
        (account["id"], trip_id),
    ).fetchone()
    if active:
        raise HTTPException(
            409, "A trip is already being planned. Wait for it to finish."
        )
    charge_account(conn, account, "plan", run_id)


def refund_unstarted_plan(repository, trip_id, run_id):
    """Refund once when provider initialization fails before model work starts."""
    with repository._connect() as conn:
        trip = conn.execute(
            "SELECT owner_id FROM trips WHERE id=%s", (trip_id,)
        ).fetchone()
        if not trip:
            return
        account = lock_account(conn, trip["owner_id"])
        job = conn.execute(
            "SELECT created_at FROM planning_jobs WHERE id=%s", (run_id,)
        ).fetchone()
        preferences = deepcopy(account["preferences"] or {})
        usage = preferences.get("demo_usage") or {}
        refunded = usage.get("refunded_runs", [])
        if not job or run_id in refunded or run_id not in usage.get("charged_runs", []):
            return
        created = datetime.fromisoformat(str(job["created_at"]).replace("Z", "+00:00"))
        if usage.get("plans_month") != created.astimezone(IST).strftime("%Y-%m"):
            return
        usage.update(
            plans_used=max(0, usage.get("plans_used", 0) - 1),
            charged_runs=[
                item for item in usage.get("charged_runs", []) if item != run_id
            ],
            refunded_runs=[*refunded[-9:], run_id],
        )
        preferences["demo_usage"] = usage
        conn.execute(
            "UPDATE users SET preferences=%s WHERE id=%s",
            (Json(preferences), account["id"]),
        )
