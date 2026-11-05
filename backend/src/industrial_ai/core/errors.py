"""Application error model mapped to a single JSON error shape."""

from fastapi import Request
from fastapi.responses import JSONResponse

from industrial_ai.core.logging import request_id_var


class AppError(Exception):
    """An error that is safe to show to the caller (message must not contain secrets)."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        retryable: bool = False,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.details = details or {}

    def to_payload(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
            "request_id": request_id_var.get(),
        }


async def app_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
    return JSONResponse(
        status_code=exc.status_code, content={"error": exc.to_payload()}, headers=headers
    )
