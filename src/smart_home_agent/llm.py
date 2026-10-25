"""Minimal OpenAI-compatible chat-completions client with tool-call support."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx


LOGGER = logging.getLogger(__name__)
MAX_RETRY_DELAY_SECONDS = 8


class LLMError(RuntimeError):
    """An LLM failure with separate internal diagnostics and user-facing copy."""

    public_message = (
        "I’m having trouble responding right now. If you asked me to change a "
        "device, please check its status before trying again."
    )

    def __init__(
        self,
        message: str,
        *,
        code: str = "model_error",
        retryable: bool = False,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


def _retry_after(response: httpx.Response, error_data: Any) -> float | None:
    header = response.headers.get("Retry-After")
    if header:
        try:
            return max(0, float(header))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(header)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(0, (retry_at - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass

    items = error_data if isinstance(error_data, list) else [error_data]
    for item in items:
        if not isinstance(item, dict):
            continue
        provider_error = item.get("error", item)
        if not isinstance(provider_error, dict):
            continue
        details = provider_error.get("details", [])
        if not isinstance(details, list):
            continue
        for detail in details:
            if not isinstance(detail, dict) or not str(detail.get("@type", "")).endswith(
                "RetryInfo"
            ):
                continue
            retry_delay = detail.get("retryDelay")
            if not isinstance(retry_delay, str):
                continue
            match = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h)", retry_delay)
            if match:
                amount = float(match.group(1))
                unit = match.group(2)
                multiplier = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
                return amount * multiplier
    return None


class OpenAICompatibleClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 45,
    ) -> None:
        self.endpoint = f"{base_url.rstrip('/')}/chat/completions"
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        payload = json.dumps(
            {
                "model": self.model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
                "temperature": 0.2,
                "stream": False,
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(
                    self.endpoint,
                    content=payload,
                    headers=headers,
                )
                response.raise_for_status()
        except httpx.TimeoutException as error:
            raise LLMError(
                "The LLM request timed out.",
                code="provider_timeout",
                retryable=True,
            ) from error
        except httpx.HTTPStatusError as error:
            message = "The model provider rejected the request."
            provider_code = ""
            error_data: Any = {}
            try:
                error_data = error.response.json()
            except (ValueError, AttributeError):
                pass
            items = error_data if isinstance(error_data, list) else [error_data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                provider_error = item.get("error", item)
                if not isinstance(provider_error, dict):
                    continue
                provider_code = str(provider_error.get("status", provider_code))
                detail = provider_error.get("message")
                if isinstance(detail, str) and detail.strip():
                    message = detail.strip()[:1500]
                    break
            status_code = error.response.status_code
            failure_code = (
                "provider_quota_exceeded"
                if status_code == 429 or provider_code == "RESOURCE_EXHAUSTED"
                else f"provider_http_{status_code}"
            )
            raise LLMError(
                f"LLM request failed with HTTP {status_code} "
                f"({provider_code or 'provider error'}): {message}",
                code=failure_code,
                retryable=(
                    status_code in {408, 425, 429}
                    or 500 <= status_code <= 599
                ),
                retry_after_seconds=_retry_after(error.response, error_data),
            ) from error
        except httpx.RequestError as error:
            raise LLMError(
                "Could not reach the configured LLM.",
                code="provider_connection",
                retryable=True,
            ) from error

        try:
            data = response.json()
            choices = data["choices"]
            message = choices[0]["message"]
            if not isinstance(message, dict):
                raise TypeError("message is not an object")
            return message
        except (
            ValueError,
            KeyError,
            IndexError,
            TypeError,
        ) as error:
            raise LLMError(
                "The LLM provider returned an invalid chat-completions response.",
                code="provider_invalid_response",
                retryable=True,
            ) from error


class ResilientLLMClient:
    def __init__(
        self,
        providers: Sequence[tuple[str, OpenAICompatibleClient]],
        *,
        max_retries: int = 1,
        retry_backoff_seconds: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not providers:
            raise ValueError("At least one configured LLM provider is required.")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative.")
        self.providers = tuple(providers)
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.sleep = sleep
        self._active_provider: ContextVar[str | None] = ContextVar(
            f"llm_active_provider_{id(self)}",
            default=None,
        )
        self._in_turn: ContextVar[bool] = ContextVar(
            f"llm_in_turn_{id(self)}",
            default=False,
        )

    @contextmanager
    def turn(self) -> Generator[None, None, None]:
        provider_token = self._active_provider.set(None)
        turn_token = self._in_turn.set(True)
        try:
            yield
        finally:
            self._in_turn.reset(turn_token)
            self._active_provider.reset(provider_token)

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        active_provider = self._active_provider.get() if self._in_turn.get() else None
        providers = (
            tuple(item for item in self.providers if item[0] == active_provider)
            if active_provider
            else self.providers
        )
        if not providers:
            raise LLMError(
                "The active LLM provider is no longer configured.",
                code="provider_configuration",
            )

        failures: list[tuple[str, LLMError]] = []
        for provider_name, client in providers:
            for attempt in range(self.max_retries + 1):
                try:
                    result = client.complete(messages, tools)
                    if self._in_turn.get():
                        self._active_provider.set(provider_name)
                    return result
                except LLMError as error:
                    failures.append((provider_name, error))
                    can_retry = error.retryable and attempt < self.max_retries
                    delay = (
                        error.retry_after_seconds
                        if error.retry_after_seconds is not None
                        else self.retry_backoff_seconds * (2**attempt)
                    )
                    if can_retry and delay <= MAX_RETRY_DELAY_SECONDS:
                        LOGGER.warning(
                            "LLM provider %s failed (%s); retrying attempt %s/%s.",
                            provider_name,
                            error.code,
                            attempt + 2,
                            self.max_retries + 1,
                        )
                        self.sleep(delay)
                        continue
                    LOGGER.warning(
                        "LLM provider %s failed (%s); moving to the next provider.",
                        provider_name,
                        error.code,
                    )
                    break

        details = "; ".join(
            f"{provider}: {error.code}" for provider, error in failures
        )
        raise LLMError(
            f"All configured LLM providers failed ({details}).",
            code="provider_failover_exhausted",
        ) from failures[-1][1]
