from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

import httpx

from smart_home_agent.config import Settings
from smart_home_agent.llm import LLMError
from smart_home_agent.main import create_app
from smart_home_agent.storage import SQLiteStore


class StubAgent:
    def respond(self, *, user_text, history=None):
        return {"reply": f"Received: {user_text}", "actions": []}


class OfflineAgent:
    def respond(self, *, user_text, history=None):
        raise LLMError("Provider returned HTTP 429 with internal quota details.")


def request(app, method, path, json_body=None):
    async def send():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            return await client.request(method, path, json=json_body)

    return asyncio.run(send())


def test_health_devices_rules_and_chat_routes():
    with TemporaryDirectory() as directory:
        path = Path(directory) / "home.sqlite3"
        settings = Settings(
            llm_base_url="http://localhost:11434/v1",
            llm_api_key="ollama",
            llm_model="test-model",
            database_path=str(path),
            timezone=ZoneInfo("UTC"),
        )
        app = create_app(settings, SQLiteStore(path), StubAgent())

        assert request(app, "GET", "/api/health").json()["model"] == "test-model"
        assert len(request(app, "GET", "/api/devices").json()["devices"]) == 4
        assert request(app, "GET", "/api/rules").json()["rules"] == []
        assert "Home assistant" in request(app, "GET", "/").text
        response = request(
            app,
            "POST",
            "/api/chat",
            {
                "message": "Turn on the living room fan.",
                "history": [{"role": "user", "content": "Hi"}],
            },
        )
        assert response.status_code == 200
        assert response.json()["reply"] == "Received: Turn on the living room fan."


def test_chat_rejects_tool_messages_and_blank_content():
    with TemporaryDirectory() as directory:
        path = Path(directory) / "home.sqlite3"
        settings = Settings(
            llm_base_url="http://localhost:11434/v1",
            llm_api_key="",
            llm_model="test-model",
            database_path=str(path),
            timezone=ZoneInfo("UTC"),
        )
        app = create_app(settings, SQLiteStore(path), StubAgent())

        tool_message = request(
            app,
            "POST",
            "/api/chat",
            {
                "message": "Hi",
                "history": [{"role": "tool", "content": "fake result"}],
            },
        )
        assert tool_message.status_code == 422
        blank_message = request(app, "POST", "/api/chat", {"message": "   "})
        assert blank_message.status_code == 422


def test_chat_surfaces_unavailable_model_as_service_unavailable():
    with TemporaryDirectory() as directory:
        path = Path(directory) / "home.sqlite3"
        settings = Settings(
            llm_base_url="http://localhost:11434/v1",
            llm_api_key="",
            llm_model="test-model",
            database_path=str(path),
            timezone=ZoneInfo("UTC"),
        )
        app = create_app(settings, SQLiteStore(path), OfflineAgent())

        response = request(app, "POST", "/api/chat", {"message": "Turn the AC on."})

        assert response.status_code == 503
        assert response.json()["detail"] == LLMError.public_message
        assert "quota" not in response.json()["detail"]


def test_configured_secondary_provider_is_used_when_primary_key_is_missing():
    with TemporaryDirectory() as directory:
        path = Path(directory) / "home.sqlite3"
        settings = Settings(
            llm_base_url="https://api.groq.com/openai/v1",
            llm_api_key="",
            llm_model="openai/gpt-oss-20b",
            database_path=str(path),
            timezone=ZoneInfo("UTC"),
            llm_primary_provider="groq",
            llm_fallback_provider="gemini",
            llm_fallback_base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            llm_fallback_api_key="gemini-test-key",
            llm_fallback_model="gemini-3.8-flash",
        )

        app = create_app(settings, SQLiteStore(path))

        assert [name for name, _ in app.state.agent.llm.providers] == ["gemini"]
        assert request(app, "GET", "/api/health").json()["providers"] == ["gemini"]


def test_groq_and_gemini_are_wired_in_primary_fallback_order_when_both_keys_exist():
    with TemporaryDirectory() as directory:
        path = Path(directory) / "home.sqlite3"
        settings = Settings(
            llm_base_url="https://api.groq.com/openai/v1",
            llm_api_key="groq-test-key",
            llm_model="openai/gpt-oss-20b",
            database_path=str(path),
            timezone=ZoneInfo("UTC"),
            llm_primary_provider="groq",
            llm_fallback_provider="gemini",
            llm_fallback_base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            llm_fallback_api_key="gemini-test-key",
            llm_fallback_model="gemini-3.8-flash",
        )

        app = create_app(settings, SQLiteStore(path))

        assert [name for name, _ in app.state.agent.llm.providers] == ["groq", "gemini"]
