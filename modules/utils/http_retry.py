"""Automatic retries for requests to AI services (OCR and translation).

A 429 (rate limit) or 503 (overloaded) from an LLM API is almost always
temporary: sending the same request again a few seconds later usually works.
Instead of failing the whole operation on the first one, post_with_retry()
waits and tries again - honoring the server's Retry-After when it sends one -
for up to RETRY_BUDGET_SECONDS. Only if the service is still failing after
that does it give up, with TransientAPIError, which the UI shows as a
non-blocking message instead of a dialog that has to be dismissed.
"""

from __future__ import annotations

import logging
import time

import requests

logger = logging.getLogger(__name__)

# Statuses worth retrying: rate limited, or the server temporarily failing.
RETRY_STATUSES = {429, 500, 502, 503, 504}
# Total time spent retrying one request, waits included, before giving up.
RETRY_BUDGET_SECONDS = 90
# Waits between attempts when the server doesn't say how long: 2, 4, 8, 16, 20, 20...
_BACKOFF_START = 2.0
_BACKOFF_MAX = 20.0


class TransientAPIError(RuntimeError):
    """An AI service kept answering with a temporary error (429/5xx), or kept
    timing out / refusing connections, for the whole retry budget."""

    def __init__(self, message: str, status_code: int | None = None, attempts: int = 1):
        super().__init__(message)
        self.status_code = status_code
        self.attempts = attempts


def _retry_after_seconds(response: requests.Response) -> float | None:
    value = response.headers.get("Retry-After") if response is not None else None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # HTTP-date form: fall back to the normal backoff


def _service_name(url: str) -> str:
    try:
        return url.split("://", 1)[-1].split("/", 1)[0]
    except Exception:
        return url


def post_with_retry(url: str, *, budget: float = RETRY_BUDGET_SECONDS, session=None, **kwargs) -> requests.Response:
    """requests.post() that retries temporary failures for up to `budget` seconds.

    Returns the response as soon as it's anything other than a retryable
    status (success or a permanent error - the caller handles those exactly
    as before). Raises TransientAPIError if it's still a retryable status, a
    timeout or a connection error when the budget runs out.
    """
    post = session.post if session is not None else requests.post
    service = _service_name(url)
    start = time.monotonic()
    delay = _BACKOFF_START
    attempt = 0
    while True:
        attempt += 1
        try:
            response = post(url, **kwargs)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            response, problem, status = None, type(e).__name__, None
        else:
            if response.status_code not in RETRY_STATUSES:
                return response
            problem, status = f"HTTP {response.status_code}", response.status_code

        wait = _retry_after_seconds(response) if response is not None else None
        if wait is None:
            wait = delay
            delay = min(delay * 2, _BACKOFF_MAX)
        elapsed = time.monotonic() - start
        if elapsed + wait > budget:
            detail = ""
            if response is not None:
                detail = (response.text or "")[:300]
            logger.warning(
                "AI request to %s still failing after %d attempts (%.0f s): %s %s",
                service, attempt, elapsed, problem, detail,
            )
            raise TransientAPIError(
                f"{service} is busy or unavailable ({problem}) - gave up after "
                f"{attempt} attempts in {elapsed:.0f} s",
                status_code=status,
                attempts=attempt,
            )
        logger.info(
            "AI request to %s: %s, retrying in %.0f s (attempt %d)",
            service, problem, wait, attempt + 1,
        )
        time.sleep(wait)
