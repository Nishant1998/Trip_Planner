"""Server-side identity, Google OAuth, and trip-ownership checks."""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from pydantic import BaseModel, ConfigDict

from app.config import get_admin_emails
from app.db.repository import TripRepository
from app.db.tables import TripRow, UserRow


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    id: int
    email: str
    username: str | None = None
    name: str | None = None
    picture: str | None = None
    role: str = "user"
    phone_number: str | None = None
    dob: str | None = None
    gender: str | None = None
    citizenship: str | None = None
    spending_tier: str = "standard"
    home_city: str | None = None


class GoogleAuthRequest(BaseModel):
    id_token: str


class TestAuthRequest(BaseModel):
    """Optional display-name override for the local test account."""

    name: str | None = None


class LogoutRequest(BaseModel):
    session_token: str | None = None


class UserResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int
    email: str
    name: str | None = None
    username: str | None = None
    picture: str | None = None
    role: str = "user"


class AuthResponse(BaseModel):
    user: UserResponse
    # Browser sessions are delivered in an HttpOnly cookie. Keeping this field
    # optional avoids breaking non-browser clients during the migration.
    session_token: str | None = None


router = APIRouter(prefix="/api/auth", tags=["auth"])
SESSION_COOKIE = "riya_session"
SESSION_MAX_AGE_SECONDS = 30 * 24 * 60 * 60


@router.get("/test/status")
async def test_auth_status() -> dict[str, bool]:
    """Expose whether the local test login control is enabled."""
    return {"enabled": _test_mode()}


def _development_mode() -> bool:
    return os.getenv("RIYA_AUTH_MODE", "google").strip().lower() == "development"


def _test_mode() -> bool:
    return os.getenv("TEST_MODE", "false").strip().lower() in {"1", "true", "yes", "on"}


def _production_mode() -> bool:
    return os.getenv("APP_ENV", "development").strip().lower() == "production"


def _set_session_cookie(response: Response, token: str) -> None:
    """Store the opaque server session where browser JavaScript cannot read it."""

    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        secure=_production_mode(),
        samesite="lax",
        path="/",
    )


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@router.post("/google", response_model=AuthResponse)
async def google_login(
    payload: GoogleAuthRequest, request: Request, response: Response
) -> AuthResponse:
    """Verify Google ID token, upsert user, and create a 30-day session token."""
    repository: TripRepository = request.app.state.repository
    raw_token = payload.id_token.strip()
    if not raw_token:
        raise HTTPException(status_code=400, detail="Missing Google ID token")

    audience = (
        os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
        or os.getenv("VITE_GOOGLE_CLIENT_ID", "").strip()
    )

    # In development mode, allow mock token bypass
    if _development_mode() and (not audience or raw_token.startswith("mock-")):
        dev_user = repository.get_user(1) or repository.get_user_by_email(
            "dev@riya.local"
        )
        if dev_user is None:
            user_id = repository.create_user(
                email="dev@riya.local",
                picture=None,
                username="Developer",
                auth_provider="google",
                auth_provider_id="dev-1",
            )
            dev_user = repository.get_user(user_id)
        assert dev_user is not None
        token = secrets.token_urlsafe(32)
        exp = (datetime.now(UTC) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        repository.create_user_session(dev_user.id, _hash_token(token), exp)
        _set_session_cookie(response, token)
        return AuthResponse(
            user=UserResponse(
                id=dev_user.id,
                email=dev_user.email,
                name=dev_user.name or dev_user.username or "Developer",
                username=dev_user.username,
                picture=dev_user.picture,
                role=dev_user.role,
            ),
        )

    if not audience:
        raise HTTPException(
            status_code=503, detail="Google OAuth is not configured on server"
        )

    try:
        claims = id_token.verify_oauth2_token(
            raw_token, google_requests.Request(), audience
        )
    except Exception as exc:
        raise HTTPException(
            status_code=401, detail="Invalid or expired Google token"
        ) from exc

    if claims.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}:
        raise HTTPException(status_code=401, detail="Invalid Google token issuer")

    subject = str(claims.get("sub", ""))
    email = str(claims.get("email", "")).strip().casefold()
    if not subject or not email or not claims.get("email_verified"):
        raise HTTPException(
            status_code=401, detail="Google account email is not verified"
        )

    name = claims.get("name")
    picture = claims.get("picture")

    # Determine role (Admin bypass)
    admin_emails = get_admin_emails()
    user_role = "admin" if email in admin_emails else "user"

    user = repository.get_user_by_auth_provider("google", subject)
    if user is None:
        user = repository.get_user_by_email(email)

    if user is None:
        user_id = repository.create_user(
            email=email,
            picture=picture,
            username=name or email.split("@")[0],
            auth_provider="google",
            auth_provider_id=subject,
        )
        # Update name and role
        repository.update_user_profile(user_id, name=name, role=user_role)
        user = repository.get_user(user_id)
    else:
        # Sync name/picture and update role if admin
        repository.update_user_profile(
            user.id, name=name, picture=picture, role=user_role
        )
        user = repository.get_user(user.id)

    if user is None:
        raise HTTPException(status_code=500, detail="Failed to initialize user record")

    # Generate 30-day session token
    session_token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(UTC) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    repository.create_user_session(user.id, _hash_token(session_token), expires_at)
    _set_session_cookie(response, session_token)

    return AuthResponse(
        user=UserResponse(
            id=user.id,
            email=user.email,
            name=user.name or name or user.username,
            username=user.username,
            picture=user.picture or picture,
            role=user_role,
        ),
    )


@router.post("/test", response_model=AuthResponse)
async def test_login(
    payload: TestAuthRequest, request: Request, response: Response
) -> AuthResponse:
    """Sign in the deterministic local test account; unavailable outside test mode."""
    if not _test_mode():
        raise HTTPException(status_code=404, detail="Test authentication is disabled")

    repository: TripRepository = request.app.state.repository
    user = repository.get_user_by_auth_provider("test", "test-user")
    if user is None:
        user = repository.get_user_by_email("test@admin.local")
    if user is None:
        user_id = repository.create_user(
            email="test@admin.local",
            picture=None,
            username=payload.name or "Test Admin",
            auth_provider="test",
            auth_provider_id="test-user",
        )
        repository.update_user_profile(user_id, role="admin")
        user = repository.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=500, detail="Failed to initialize test user")

    session_token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(UTC) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    repository.create_user_session(user.id, _hash_token(session_token), expires_at)
    _set_session_cookie(response, session_token)
    return AuthResponse(
        user=UserResponse(
            id=user.id,
            email=user.email,
            name=user.name or user.username or "Test Admin",
            username=user.username,
            picture=user.picture,
            role=user.role,
        ),
    )


@router.get("/test-mode")
async def test_mode_status() -> dict[str, bool]:
    """Expose only whether the local test-login control should be shown."""
    return {"test_mode": _test_mode()}


@router.post("/logout")
async def logout(
    payload: LogoutRequest, request: Request, response: Response
) -> dict[str, bool]:
    """Revoke an active session token."""
    repository: TripRepository = request.app.state.repository
    token = request.cookies.get(SESSION_COOKIE) or payload.session_token
    if token:
        repository.delete_user_session(_hash_token(token))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"success": True}


async def current_user(
    request: Request,
    authorization: str | None = Header(default=None),
    x_dev_user_id: int | None = Header(default=None),
) -> AuthenticatedUser:
    """Resolve an authenticated user from session token or Google ID token."""
    repository: TripRepository = request.app.state.repository

    if _development_mode() and x_dev_user_id is not None:
        user = repository.get_user(x_dev_user_id)
        if user:
            return _build_authenticated_user(user)

    token = request.cookies.get(SESSION_COOKIE)
    if token:
        session_user = repository.get_user_by_session_token(_hash_token(token))
        if session_user is not None:
            return _build_authenticated_user(session_user)

    if not authorization or not authorization.startswith("Bearer "):
        # Dev fallback if development mode
        if _development_mode():
            dev_user = repository.get_user(1) or repository.get_user_by_email(
                "dev@riya.local"
            )
            if dev_user:
                return _build_authenticated_user(dev_user)
        raise HTTPException(status_code=401, detail="Authentication is required")

    token = authorization.removeprefix("Bearer ").strip()

    # 1. Check Session Token in Database
    session_user = repository.get_user_by_session_token(_hash_token(token))
    if session_user is not None:
        return _build_authenticated_user(session_user)

    # 2. Check Direct Google ID Token
    audience = (
        os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
        or os.getenv("VITE_GOOGLE_CLIENT_ID", "").strip()
    )
    if audience:
        try:
            claims = id_token.verify_oauth2_token(
                token, google_requests.Request(), audience
            )
            email = str(claims.get("email", "")).strip().casefold()
            subject = str(claims.get("sub", ""))
            user = repository.get_user_by_auth_provider(
                "google", subject
            ) or repository.get_user_by_email(email)
            if user:
                return _build_authenticated_user(user)
        except Exception:
            pass

    raise HTTPException(status_code=401, detail="Invalid or expired session token")


@router.get("/me", response_model=UserResponse)
async def get_current_user_profile(
    user: AuthenticatedUser = Depends(current_user),
) -> UserResponse:
    """Return the profile of the currently signed in user."""
    return UserResponse(
        id=user.id,
        email=user.email,
        name=user.name or user.username,
        username=user.username,
        picture=user.picture,
        role=user.role,
    )


def _build_authenticated_user(user: UserRow) -> AuthenticatedUser:
    admin_emails = get_admin_emails()
    role = (
        "admin"
        if (
            user.role == "admin"
            or (user.email and user.email.casefold() in admin_emails)
        )
        else user.role
    )
    return AuthenticatedUser(
        id=user.id,
        email=user.email,
        username=user.username,
        name=user.name or user.username,
        picture=user.picture,
        role=role,
        phone_number=user.phone_number,
        dob=str(user.dob) if user.dob else None,
        gender=user.gender,
        citizenship=user.citizenship,
        spending_tier=user.spending_tier or "standard",
        home_city=getattr(user, "home_city", None),
    )


def require_trip_owner(trip: TripRow, user: AuthenticatedUser) -> TripRow:
    """Return a trip only to its owner; hide other trip IDs as not found."""
    if trip.owner_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Trip not found"
        )
    return trip
