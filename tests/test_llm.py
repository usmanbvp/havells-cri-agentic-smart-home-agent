from __future__ import annotations

import json
from pathlib import Path

import httpx
from smart_home_agent.config import Settings
from smart_home_agent.llm import (
    LLMError,
    OpenAICompatibleClient,
    ResilientLLMClient,
)


def test_gemini_defaults_and_private_key_environment(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_BASE_URL", raising=False)
    monkeypatch.delenv("GROQ_MODEL", raising=False)
    monkeypatch.delenv("LLM_PRIMARY_PROVIDER", raising=False)
    monkeypatch.delenv("GEMINI_BASE_URL", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("SMART_HOME_DB_PATH", raising=False)
    monkeypatch.delenv("SMART_HOME_TIMEZONE", raising=False)

    settings = Settings.from_environment(tmp_path / "missing.env")

    assert settings.llm_primary_provider == "groq"
    assert settings.llm_base_url == "https://api.groq.com/openai/v1"
    assert settings.llm_model == "openai/gpt-oss-20b"
    assert settings.llm_api_key == ""
    assert settings.llm_fallback_provider == "gemini"

    monkeypatch.setenv("GEMINI_API_KEY", "local-test-key")
    settings = Settings.from_environment(tmp_path / "missing.env")

    assert settings.llm_api_key == ""
    assert settings.llm_fallback_api_key == "local-test-key"
    assert settings.llm_fallback_model == "gemini-3.8-flash"


def test_groq_primary_and_gemini_fallback_dotenv_configuration(tmp_path: Path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "LLM_PRIMARY_PROVIDER=groq\n"
        "GROQ_API_KEY=private-groq-key\n"
        "GEMINI_API_KEY=private-test-key\n"
        "GROQ_MODEL=openai/gpt-oss-20b\n"
        "GEMINI_MODEL=gemini-3.8-flash\n"
        "LLM_BASE_URL=https://legacy-gemini.example/v1\n"
        "LLM_MODEL=legacy-gemini-model\n",
        encoding="utf-8",
    )

    settings = Settings.from_environment(dotenv)

    assert settings.llm_primary_provider == "groq"
    assert settings.llm_api_key == "private-groq-key"
    assert settings.llm_base_url == "https://api.groq.com/openai/v1"
    assert settings.llm_model == "openai/gpt-oss-20b"
    assert settings.llm_fallback_provider == "gemini"
    assert settings.llm_fallback_api_key == "private-test-key"
    assert settings.llm_fallback_base_url.endswith("/openai")
    assert settings.llm_fallback_model == "gemini-3.8-flash"


def test_gemini_compatible_request_preserves_tool_calls_and_auth(monkeypatch):
    response_body = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_123",
                            "type": "function",
                            "function": {
                                "name": "list_devices",
                                "arguments": "{}",
                            },
                        }
                    ],
                }
            }
        ]
    }
    observed = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return response_body

    class FakeHTTPClient:
        def __init__(self, timeout):
            observed["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, *, content, headers):
            observed["url"] = url
            observed["authorization"] = headers["Authorization"]
            observed["payload"] = json.loads(content)
            return FakeResponse()

    monkeypatch.setattr("smart_home_agent.llm.httpx.Client", FakeHTTPClient)
    client = OpenAICompatibleClient(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key="gemini-test-key",
        model="gemini-3.8-flash",
    )
    tool_schemas = [
        {
            "type": "function",
            "function": {
                "name": "list_devices",
                "description": "List devices",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    message = client.complete(
        [{"role": "user", "content": "List my devices."}],
        tool_schemas,
    )

    assert observed["url"] == (
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    )
    assert observed["authorization"] == "Bearer gemini-test-key"
    assert observed["timeout"] == 45
    assert observed["payload"]["model"] == "gemini-3.8-flash"
    assert observed["payload"]["tool_choice"] == "auto"
    assert observed["payload"]["tools"] == tool_schemas
    assert message["tool_calls"][0]["function"]["name"] == "list_devices"


def test_llm_timeout_is_reported_explicitly(monkeypatch):
    class FakeHTTPClient:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, *args, **kwargs):
            raise httpx.ReadTimeout("provider response timed out")

    monkeypatch.setattr("smart_home_agent.llm.httpx.Client", FakeHTTPClient)
    client = OpenAICompatibleClient(
        base_url="https://example.com/v1",
        api_key="test-key",
        model="test-model",
    )

    try:
        client.complete([], [])
    except LLMError as error:
        assert "timed out" in str(error)
    else:
        raise AssertionError("Expected a timeout to raise LLMError")


class QueueLLMClient:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_resilient_client_retries_transient_error_then_fails_over():
    pauses = []
    primary = QueueLLMClient(
        LLMError("temporary network issue", code="provider_connection", retryable=True),
        LLMError("primary remained unavailable", code="provider_connection", retryable=True),
    )
    secondary = QueueLLMClient({"content": "Gemini response", "tool_calls": []})
    client = ResilientLLMClient(
        [("groq", primary), ("gemini", secondary)],
        max_retries=1,
        retry_backoff_seconds=0.25,
        sleep=pauses.append,
    )

    response = client.complete([], [])

    assert response["content"] == "Gemini response"
    assert primary.calls == 2
    assert secondary.calls == 1
    assert pauses == [0.25]


def test_resilient_client_skips_retry_when_quota_reset_is_far_away():
    pauses = []
    primary = QueueLLMClient(
        LLMError(
            "daily quota exhausted",
            code="provider_quota_exceeded",
            retryable=True,
            retry_after_seconds=3600,
        )
    )
    secondary = QueueLLMClient({"content": "Fallback response", "tool_calls": []})
    client = ResilientLLMClient(
        [("groq", primary), ("gemini", secondary)],
        max_retries=2,
        sleep=pauses.append,
    )

    response = client.complete([], [])

    assert response["content"] == "Fallback response"
    assert primary.calls == 1
    assert secondary.calls == 1
    assert pauses == []


def test_resilient_client_retries_short_retry_after_and_falls_back_on_auth_error():
    pauses = []
    primary = QueueLLMClient(
        LLMError(
            "rate limited",
            code="provider_quota_exceeded",
            retryable=True,
            retry_after_seconds=0.25,
        ),
        LLMError("invalid primary credential", code="provider_http_401"),
    )
    secondary = QueueLLMClient({"content": "Fallback response", "tool_calls": []})
    client = ResilientLLMClient(
        [("groq", primary), ("gemini", secondary)],
        max_retries=1,
        sleep=pauses.append,
    )

    response = client.complete([], [])

    assert response["content"] == "Fallback response"
    assert primary.calls == 2
    assert secondary.calls == 1
    assert pauses == [0.25]


def test_resilient_client_pins_provider_after_first_response_in_turn():
    primary = QueueLLMClient(
        {"content": None, "tool_calls": [{"id": "call_1"}]},
        LLMError("provider timed out", code="provider_timeout", retryable=True),
    )
    secondary = QueueLLMClient({"content": "Must not switch after a tool call"})
    client = ResilientLLMClient(
        [("groq", primary), ("gemini", secondary)],
        max_retries=0,
    )

    with client.turn():
        first = client.complete([], [])
        assert first["tool_calls"]
        try:
            client.complete([], [])
        except LLMError as error:
            assert error.code == "provider_failover_exhausted"
        else:
            raise AssertionError("Expected the pinned provider failure to surface")

    assert primary.calls == 2
    assert secondary.calls == 0


def test_gemini_quota_error_from_wrapped_response_is_actionable(monkeypatch):
    request = httpx.Request("POST", "https://example.com/v1/chat/completions")
    response = httpx.Response(
        429,
        json=[
            {
                "error": {
                    "code": 429,
                    "message": (
                        "You exceeded your current quota. Generate content requests "
                        "per day limit exceeded on Free Tier. Please retry in 11h."
                    ),
                    "status": "RESOURCE_EXHAUSTED",
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.RetryInfo",
                            "retryDelay": "40355s",
                        }
                    ],
                }
            }
        ],
        request=request,
    )

    class FakeHTTPClient:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, *args, **kwargs):
            return response

    monkeypatch.setattr("smart_home_agent.llm.httpx.Client", FakeHTTPClient)
    client = OpenAICompatibleClient(
        base_url="https://example.com/v1",
        api_key="test-key",
        model="test-model",
    )

    try:
        client.complete([], [])
    except LLMError as error:
        assert "HTTP 429" in str(error)
        assert "current quota" in str(error)
        assert "retry in 11h" in str(error)
        assert error.code == "provider_quota_exceeded"
        assert error.retry_after_seconds == 40355
        assert "daily limit" not in error.public_message
    else:
        raise AssertionError("Expected the provider quota response to raise LLMError")
