import logging
from typing import Annotated

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

from buyback.config import settings

logger = logging.getLogger(__name__)

API_KEY_SCHEME = APIKeyHeader(name="X-API-Key", auto_error=False)


def api_key_dependency(x_api_key: Annotated[str | None, Security(API_KEY_SCHEME)] = None) -> str:
    if not settings.COMMON_API_KEY:
        return ""
    if x_api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing API key", headers={"WWW-Authenticate": "ApiKey"}
        )
    if x_api_key != settings.COMMON_API_KEY:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid API key")
    return x_api_key


AuthDep = Annotated[str, Security(api_key_dependency)]
