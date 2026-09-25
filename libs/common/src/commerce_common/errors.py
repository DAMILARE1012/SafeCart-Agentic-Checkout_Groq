"""One error model for every service: ``{code, message, retryable}``.

Domain code raises ``DomainError`` subclasses; the app factory turns them into
consistent JSON responses. The same shape reaches the agent (as tool errors it
can correct) and the widget.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import DBAPIError

log = structlog.get_logger(__name__)


class ErrorBody(BaseModel):
    code: str
    message: str
    retryable: bool = False
    details: Any | None = None


class DomainError(Exception):
    http_status = 422
    retryable = False

    def __init__(self, code: str, message: str, *, details: Any | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def body(self) -> ErrorBody:
        return ErrorBody(code=self.code, message=self.message, retryable=self.retryable, details=self.details)


class ValidationFailed(DomainError):
    http_status = 422


class BadRequest(DomainError):
    http_status = 400


class NotFound(DomainError):
    http_status = 404


class Conflict(DomainError):
    http_status = 409


class Unauthorized(DomainError):
    http_status = 401


class Forbidden(DomainError):
    http_status = 403


class ServiceUnavailable(DomainError):
    http_status = 503
    retryable = True


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.body().model_dump(exclude_none=True))

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        body = ErrorBody(
            code="validation_error",
            message="Request validation failed",
            details=[{"loc": e.get("loc"), "msg": e.get("msg")} for e in exc.errors()],
        )
        return JSONResponse(status_code=422, content=body.model_dump(exclude_none=True))

    @app.exception_handler(DBAPIError)
    async def _database(_: Request, exc: DBAPIError) -> JSONResponse:
        # A handler for this specific type, not the catch-all below: Starlette's outermost error middleware
        # (which serves the catch-all) re-raises after responding, logging a crash for bad client input.
        if _is_unstorable_text(exc):
            # Input with characters the database cannot store (e.g. NUL), found by API fuzzing.
            body = ErrorBody(
                code="invalid_characters", message="The request contains characters that are not allowed"
            )
            return JSONResponse(status_code=400, content=body.model_dump(exclude_none=True))
        log.exception("database_error", error_type=type(exc.orig).__name__)
        body = ErrorBody(code="internal_error", message="Unexpected error", retryable=True)
        return JSONResponse(status_code=500, content=body.model_dump(exclude_none=True))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error", error_type=type(exc).__name__)
        body = ErrorBody(code="internal_error", message="Unexpected error", retryable=True)
        return JSONResponse(status_code=500, content=body.model_dump(exclude_none=True))


_UNSTORABLE = ("CharacterNotInRepertoireError", "UntranslatableCharacterError", "invalid byte sequence")


def _is_unstorable_text(exc: BaseException) -> bool:
    """PostgreSQL refused a string (found by API fuzzing: a NUL byte in a cart's conversation id)."""
    return any(marker in f"{type(exc).__name__}: {exc}" for marker in _UNSTORABLE)
