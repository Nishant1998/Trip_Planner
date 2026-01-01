from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/api/health")
@router.get("/health")
@router.get("/health/live")
async def live() -> dict[str, str]:
    """Report process liveness without checking external services."""
    return {"status": "ok"}
