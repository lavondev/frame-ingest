"""Typed errors with human-readable messages, and secret redaction for text and log records."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any

_KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-*.]{6,}")


def redact(text: str, *secrets: str | None) -> str:
    """Strip API keys (exact values and sk-… patterns) from any text we store or emit."""
    for secret in secrets:
        if secret and len(secret) >= 8:
            text = text.replace(secret, "[redacted]")
    return _KEY_PATTERN.sub("sk-[redacted]", text)


def install_log_redaction(secrets: Iterable[str | None]) -> None:
    """Belt and braces: even if some library logs a request or error with a key in it, the key
    is stripped from every log record. Records are only touched when a secret is actually
    present, so structured records (formatted from `args`) are left intact."""
    known = [s for s in secrets if s]
    previous = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        try:
            message = record.getMessage()
            cleaned = redact(message, *known)
            if cleaned != message:
                record.msg, record.args = cleaned, ()
        except Exception:  # noqa: S110  # pragma: no cover - never break logging
            pass
        return record

    logging.setLogRecordFactory(factory)


class FrameIngestError(Exception):
    code = "pipeline_failed"
    status = 500

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status


class MediaError(FrameIngestError):
    """The uploaded file is unsupported or corrupt."""

    code = "unsupported_media"
    status = 415


class FatalProviderError(FrameIngestError):
    """Errors that retrying or continuing cannot fix (bad key, no quota, missing model)."""

    code = "provider_error"
    status = 502


class ProviderError(FrameIngestError):
    """A non-fatal provider failure for one unit of work (fail-soft stages record and go on)."""

    code = "provider_error"
    status = 502


class ValidationFailure(ProviderError):
    """Model output failed validation (bad IDs, bad schema, ...); retryable."""

    code = "invalid_model_output"


class CapabilityChanged(Exception):
    """The provider revealed a capability the plan did not assume (e.g. no segment timestamps).

    The runner invalidates the affected stages and re-plans.
    """

    def __init__(self, capability: str) -> None:
        super().__init__(capability)
        self.capability = capability


class JobNotFound(FrameIngestError):
    code = "job_not_found"
    status = 404

    def __init__(self, job_id: str) -> None:
        super().__init__(f"Job '{job_id}' was not found.")


class JobBusy(FrameIngestError):
    code = "job_running"
    status = 409
