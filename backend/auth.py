import hmac

from fastapi import HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

security = HTTPBearer(auto_error=False)


async def require_api_key(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    from backend.config import get_settings
    try:
        expected_key = get_settings().nexus_api_key
    except (KeyError, RuntimeError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Vault not configured",
        )
    # Compare as bytes: str compare_digest raises TypeError on non-ASCII input
    # (a malformed token must 401, not 500).
    if (credentials is None or not expected_key
            or not hmac.compare_digest(
                credentials.credentials.encode(), expected_key.encode())):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials.credentials
