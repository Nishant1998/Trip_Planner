"""User profile management API endpoints with database persistence."""

from __future__ import annotations

from typing import Any
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from app.api.auth import AuthenticatedUser, current_user
from app.db.repository import TripRepository


router = APIRouter(prefix="/api/users", tags=["users"])


class UserProfileResponse(BaseModel):
    """User profile response matching Profile.tsx fields."""

    model_config = ConfigDict(extra="ignore")

    id: str
    email: str
    username: str
    name: str
    phone_number: str | None = None
    dob: str | None = None
    gender: str = "prefer_not_to_say"
    citizenship: str | None = None
    spendingTier: str = "standard"
    authProvider: str = "google"
    photoURL: str = ""
    home_city: str | None = None
    role: str = "user"


class UserProfileUpdateRequest(BaseModel):
    """Profile edit request from Profile.tsx."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    username: str | None = None
    phone_number: str | None = None
    dob: str | None = None
    gender: str | None = None
    citizenship: str | None = None
    spendingTier: str | None = None
    spending_tier: str | None = None
    home_city: str | None = None


@router.get("/profile", response_model=UserProfileResponse)
async def get_profile(
    user: AuthenticatedUser = Depends(current_user),
) -> UserProfileResponse:
    """Return the authenticated user's profile from database."""
    return UserProfileResponse(
        id=str(user.id),
        email=user.email,
        username=user.username or user.email.split("@")[0],
        name=user.name or user.username or "Traveler",
        phone_number=user.phone_number,
        dob=str(user.dob) if user.dob else None,
        gender=user.gender or "prefer_not_to_say",
        citizenship=user.citizenship,
        spendingTier=user.spending_tier or "standard",
        authProvider="google",
        photoURL=user.picture or "",
        home_city=user.home_city,
        role=user.role,
    )


@router.put("/profile")
async def update_profile(
    payload: UserProfileUpdateRequest,
    request: Request,
    user: AuthenticatedUser = Depends(current_user),
) -> dict[str, Any]:
    """Update editable user profile fields in Neon PostgreSQL database."""
    repository: TripRepository = request.app.state.repository

    update_data: dict[str, Any] = {}
    if payload.name is not None:
        update_data["name"] = payload.name.strip() or user.name
    if payload.username is not None:
        update_data["username"] = payload.username.strip() or user.username
    if payload.phone_number is not None:
        update_data["phone_number"] = payload.phone_number.strip() or None
    if payload.dob is not None:
        val = payload.dob.strip()
        update_data["dob"] = val if val else None
    if payload.gender is not None:
        update_data["gender"] = payload.gender
    if payload.citizenship is not None:
        update_data["citizenship"] = payload.citizenship.strip() or None

    tier = payload.spendingTier or payload.spending_tier
    if tier is not None:
        update_data["spending_tier"] = tier

    if payload.home_city is not None:
        update_data["home_city"] = payload.home_city.strip() or None

    updated_user = repository.update_user_profile(user.id, **update_data)
    if updated_user is None:
        raise HTTPException(status_code=404, detail="User not found")

    return {
        "success": True,
        "message": "Profile updated successfully",
        "profile": {
            "id": str(updated_user.id),
            "email": updated_user.email,
            "username": updated_user.username or "",
            "name": updated_user.name or "",
            "phone_number": updated_user.phone_number,
            "dob": str(updated_user.dob) if updated_user.dob else None,
            "gender": updated_user.gender or "prefer_not_to_say",
            "citizenship": updated_user.citizenship,
            "spendingTier": updated_user.spending_tier or "standard",
            "authProvider": "google",
            "photoURL": updated_user.picture or "",
            "home_city": getattr(updated_user, "home_city", None),
            "role": updated_user.role,
        },
    }
