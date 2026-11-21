"""Thin HTTP adapter for OpenAI Decisions, System One (Jev / Liquid d1), and Gemini Decisions APIs."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from agent_browser_decisions.core import (
    PROVIDERS,
    DecisionConfig,
    extract_input_tokens,
    redact_text,
    resolve_decision_config,
)

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504, 529})
MAX_RETRY_AFTER_S = 5.0


class MissingKey(RuntimeError):
    """Raised when Tier 2 is needed but the selected provider's API key is not configured."""


class DecisionsError(RuntimeError):
    """Raised on non-retryable or exhausted HTTP errors from the decision model API."""


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
    """Context-managed client for OpenAI, Gemini, TypeSafe (Jev), and Liquid (d1) decision APIs."""

    def __init__(
        self,
        *,
        provider: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        environ: Mapping[str, str] | None = None,
        timeout_s: float = 30.0,
        max_retries: int = 2,
        base_backoff_s: float = 0.5,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        on_metric: Callable[[str, float, int], None] | None = None,
    ) -> None:
        self._provider_arg = provider
        self._api_key_arg = api_key
        self._base_url_arg = base_url
        self._model_arg = model
        self._environ = environ
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.base_backoff_s = base_backoff_s
        self._sleep = sleep
        self._on_metric = on_metric
        self._external_client = client is not None
        self._client = client

        cfg = self._resolve_config()
        self.provider = cfg.provider
        self.protocol = cfg.protocol
        self.base_url = cfg.base_url
        self.endpoint_url = cfg.endpoint_url
        self.model = cfg.model

    def _resolve_config(self) -> DecisionConfig:
        env = os.environ if self._environ is None else self._environ
        return resolve_decision_config(
            provider=self._provider_arg,
            model=self._model_arg,
            base_url=self._base_url_arg,
            api_key=self._api_key_arg,
            environ=env,
        )

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
        cfg = self._resolve_config()
        self.provider = cfg.provider
        self.protocol = cfg.protocol
        self.base_url = cfg.base_url
        self.endpoint_url = cfg.endpoint_url
        self.model = cfg.model

        if not cfg.api_key:
            expected = " / ".join(PROVIDERS[cfg.provider].api_key_envs)
            raise MissingKey(
                f"{expected} is required for provider {cfg.provider!r}: a step needs the decision model "
                "(no unique label match)"
            )

        headers = {cfg.auth_header: f"{cfg.auth_prefix}{cfg.api_key}"}
        payload = request if "model" in request else {**request, "model": cfg.model}
        secrets = {cfg.api_key}

        for attempt in range(self.max_retries + 1):
            started = time.perf_counter()
            try:
                response = self._http().post(cfg.endpoint_url, headers=headers, json=payload)
            except (httpx.TimeoutException, httpx.NetworkError) as err:
                elapsed = time.perf_counter() - started
                if self._on_metric is not None:
                    self._on_metric("decision", elapsed, 0)
                if attempt < self.max_retries:
                    self._sleep(min(self.base_backoff_s * (2**attempt), MAX_RETRY_AFTER_S))
                    continue
                msg = redact_text(str(err), secrets)
                raise DecisionsError(
                    f"Decisions API ({cfg.provider}) network/timeout error after {attempt + 1} attempts: {msg}"
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
                snippet = redact_text(response.text.strip().replace("\n", " ")[:200], secrets)
                raise DecisionsError(f"Decisions API ({cfg.provider}) HTTP {response.status_code}: {snippet}")
            body = response.json()
            tokens = extract_input_tokens(body)
            if self._on_metric is not None:
                self._on_metric("decision", elapsed, tokens)
            return body
        raise DecisionsError(f"Decisions API ({cfg.provider}) exhausted retries")
