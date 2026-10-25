"""FastAPI application and local web-chat entry point."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import SmartHomeAgent
from .config import Settings
from .llm import LLMError, OpenAICompatibleClient, ResilientLLMClient
from .storage import SQLiteStore
from .tools import SmartHomeTools


LOGGER = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    history: list[ChatMessage] = Field(default_factory=list, max_length=12)


def create_app(
    settings: Settings | None = None,
    store: SQLiteStore | None = None,
    agent: SmartHomeAgent | None = None,
) -> FastAPI:
    settings = settings or Settings.from_environment()
    store = store or SQLiteStore(settings.database_path)
    store.initialize()
    provider_names: list[str] = []
    if agent is None:
        providers: list[tuple[str, OpenAICompatibleClient]] = []
        if (
            settings.llm_api_key
            or not settings.llm_fallback_api_key
            or settings.llm_primary_provider == "custom"
        ):
            provider_names.append(settings.llm_primary_provider)
            providers.append(
                (
                    settings.llm_primary_provider,
                    OpenAICompatibleClient(
                        base_url=settings.llm_base_url,
                        api_key=settings.llm_api_key,
                        model=settings.llm_model,
                        timeout_seconds=settings.llm_timeout_seconds,
                    ),
                )
            )
        if settings.llm_fallback_provider and settings.llm_fallback_api_key:
            provider_names.append(settings.llm_fallback_provider)
            providers.append(
                (
                    settings.llm_fallback_provider,
                    OpenAICompatibleClient(
                        base_url=settings.llm_fallback_base_url,
                        api_key=settings.llm_fallback_api_key,
                        model=settings.llm_fallback_model,
                        timeout_seconds=settings.llm_timeout_seconds,
                    ),
                )
            )
        llm = ResilientLLMClient(
            providers,
            max_retries=settings.llm_max_retries,
            retry_backoff_seconds=settings.llm_retry_backoff_seconds,
        )
        agent = SmartHomeAgent(
            llm,
            SmartHomeTools(store, settings.timezone),
        )
    if not provider_names:
        provider_names = [settings.llm_primary_provider]
    health_model = (
        settings.llm_fallback_model
        if provider_names[0] != settings.llm_primary_provider
        else settings.llm_model
    )

    application = FastAPI(
        title="Havells Smart Home Agent",
        version="1.0.0",
        description="A tool-using mock home agent with persistent personalization rules.",
    )
    application.state.agent = agent
    application.state.store = store
    application.state.settings = settings
    application.mount(
        "/static",
        StaticFiles(directory=STATIC_DIR),
        name="static",
    )

    @application.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @application.get("/api/health")
    def health() -> dict[str, str | list[str]]:
        return {
            "status": "ok",
            "model": health_model,
            "providers": provider_names,
            "timezone": str(settings.timezone),
        }

    @application.get("/api/devices")
    def devices() -> dict[str, list[dict[str, object]]]:
        return {"devices": store.list_devices()}

    @application.get("/api/rules")
    def rules() -> dict[str, list[dict[str, object]]]:
        return {"rules": store.list_rules()}

    @application.post("/api/chat")
    def chat(request: ChatRequest) -> dict[str, object]:
        user_text = request.message.strip()
        if not user_text:
            raise HTTPException(status_code=422, detail="Message cannot be blank.")
        history = [
            {"role": item.role, "content": item.content}
            for item in request.history
        ]
        try:
            return agent.respond(user_text=user_text, history=history)
        except LLMError as error:
            LOGGER.warning("Chat request failed (%s).", error.code)
            raise HTTPException(
                status_code=503,
                detail=error.public_message,
            ) from error

    return application


app = create_app()
