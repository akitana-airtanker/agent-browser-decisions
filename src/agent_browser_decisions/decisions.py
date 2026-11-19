"""Thin HTTP adapter for the OpenAI Decisions API (/v1/decisions) with bounded transient retries."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

import httpx

from agent_browser_decisions.core import DEFAULT_BASE_URL, DEFAULT_DECISION_MODEL

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_S = 5.0


class MissingKey(RuntimeError):
    """Raised when Tier 2 is needed but OPENAI_API_KEY is not configured."""


class DecisionsError(RuntimeError):
    """Raised on non-retryable or exhausted HTTP errors from the Decisions API."""


def _parse_retry_after(header_value: str | None, fallback_s: float) -> float:
    if not header_value:
        return fallback_s
    try:
        seconds = float(header_value.strip())
    except ValueError:
        return fallback_s
    if seconds < 0:
        return fallback_s
    return min(seconds, MAX_RETRY_AFTER_S)


class DecisionsClient:
    """Context-managed client for OpenAI `/v1/decisions` with bounded backoff on 429/5xx/network errors."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_s: float = 30.0,
        max_retries: int = 2,
        base_backoff_s: float = 0.5,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        on_metric: Callable[[str, float, int], None] | None = None,
    ) -> None:
        self._api_key = api_key
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or os.environ.get("DECISION_MODEL") or DEFAULT_DECISION_MODEL
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.base_backoff_s = base_backoff_s
        self._sleep = sleep
        self._on_metric = on_metric
        self._external_client = client is not None
        self._client = client

    def __enter__(self) -> DecisionsClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._client is not None and not self._external_client:
            self._client.close()
            self._client = None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(http2=True, timeout=self.timeout_s)
        return self._client

    def decide(self, request: dict[str, Any]) -> dict[str, Any]:
        key = self._api_key if self._api_key is not None else os.environ.get("OPENAI_API_KEY")
        if not key:
            raise MissingKey("OPENAI_API_KEY is required: a step needs the decision model (no unique label match)")
        url = f"{self.base_url}/decisions"
        headers = {"Authorization": f"Bearer {key}"}
        payload = request if "model" in request else {**request, "model": self.model}
        for attempt in range(self.max_retries + 1):
            started = time.perf_counter()
            try:
                response = self._http().post(url, headers=headers, json=payload)
            except (httpx.TimeoutException, httpx.NetworkError) as err:
                elapsed = time.perf_counter() - started
                if self._on_metric is not None:
                    self._on_metric("decision", elapsed, 0)
                if attempt < self.max_retries:
                    self._sleep(min(self.base_backoff_s * (2**attempt), MAX_RETRY_AFTER_S))
                    continue
                raise DecisionsError(
                    f"Decisions API network/timeout error after {attempt + 1} attempts: {err}"
                ) from err
            elapsed = time.perf_counter() - started
            if response.status_code in RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                if self._on_metric is not None:
                    self._on_metric("decision", elapsed, 0)
                delay = _parse_retry_after(
                    response.headers.get("retry-after"),
                    min(self.base_backoff_s * (2**attempt), MAX_RETRY_AFTER_S),
                )
                self._sleep(delay)
                continue
            if response.status_code >= 400:
                if self._on_metric is not None:
                    self._on_metric("decision", elapsed, 0)
                snippet = response.text.strip().replace("\n", " ")[:200]
                raise DecisionsError(f"Decisions API HTTP {response.status_code}: {snippet}")
            body = response.json()
            tokens = body.get("usage", {}).get("input_tokens", 0) if isinstance(body, dict) else 0
            if self._on_metric is not None:
                self._on_metric("decision", elapsed, tokens if isinstance(tokens, int) else 0)
            return body
        raise DecisionsError("Decisions API exhausted retries")
