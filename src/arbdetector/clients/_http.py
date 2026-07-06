"""Shared HTTP plumbing for the platform clients: throttle + retry/backoff.

Small deliberate addition to the plan §10 layout (flagged 2026-07-05): both
adapters need identical GET-JSON-with-backoff behavior (plan §2.2: exponential
backoff on 429, polite request rates), so it lives once here instead of being
copy-pasted per client.
"""

from __future__ import annotations

import time
from typing import Any

import httpx


class RetryingJsonHttp:
    """GET-JSON wrapper for one host (one ``httpx.Client``): request-rate
    throttling plus exponential backoff on 429/5xx/transport errors.

    Other 4xx raise immediately (``httpx.HTTPStatusError``) — those are
    caller bugs, not upstream flakiness, and retrying them only hides them.
    """

    def __init__(
        self,
        http_client: httpx.Client,
        *,
        backoff_base_sec: float = 1.0,
        max_retries: int = 4,
        max_requests_per_sec: float = 25.0,
        error_cls: type[Exception] = RuntimeError,
    ) -> None:
        self._http = http_client
        self._backoff_base_sec = backoff_base_sec
        self._max_retries = max_retries
        self._min_request_interval = (
            1.0 / max_requests_per_sec if max_requests_per_sec > 0 else 0.0
        )
        self._last_request_at = 0.0
        self._error_cls = error_cls

    def close(self) -> None:
        self._http.close()

    def _throttle(self) -> None:
        if not self._min_request_interval:
            return
        wait = self._last_request_at + self._min_request_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET ``path`` and decode JSON (may be an object OR a bare array —
        Gamma returns arrays)."""
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            self._throttle()
            self._last_request_at = time.monotonic()
            try:
                response = self._http.get(path, params=params)
            except httpx.TransportError as exc:
                last_error = exc
            else:
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = self._error_cls(
                        f"GET {path} -> HTTP {response.status_code}"
                    )
                else:
                    response.raise_for_status()
                    return response.json()
            if attempt < self._max_retries:
                time.sleep(self._backoff_base_sec * (2**attempt))
        raise self._error_cls(
            f"GET {path} failed after {self._max_retries + 1} attempts"
        ) from last_error
