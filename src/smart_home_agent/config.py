"""Environment-backed application settings with a dependency-free .env reader."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        if not separator or not key.strip():
            raise ValueError(f"Invalid .env entry on line {line_number}.")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


@dataclass(frozen=True)
class Settings:
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    database_path: str
    timezone: ZoneInfo
    llm_primary_provider: str = "custom"
    llm_fallback_provider: str = ""
    llm_fallback_base_url: str = ""
    llm_fallback_api_key: str = ""
    llm_fallback_model: str = ""
    llm_max_retries: int = 1
    llm_timeout_seconds: float = 45
    llm_retry_backoff_seconds: float = 0.5

    @classmethod
    def from_environment(cls, dotenv_path: Path | None = None) -> Settings:
        file_values = _read_dotenv(dotenv_path or Path(".env"))

        def value(name: str, default: str) -> str:
            return os.environ.get(name, file_values.get(name, default))

        primary_provider = value("LLM_PRIMARY_PROVIDER", "groq").strip().casefold()
        provider_settings = {
            "groq": (
                value("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
                value("GROQ_API_KEY", "").strip(),
                value("GROQ_MODEL", "openai/gpt-oss-20b").strip(),
            ),
            "gemini": (
                value(
                    "GEMINI_BASE_URL",
                    "https://generativelanguage.googleapis.com/v1beta/openai/",
                ),
                value("GEMINI_API_KEY", "").strip(),
                value("GEMINI_MODEL", "gemini-3.8-flash").strip(),
            ),
        }
        if primary_provider == "custom":
            base_url = value("LLM_BASE_URL", "").strip()
            api_key = value("LLM_API_KEY", "").strip()
            model = value("LLM_MODEL", "").strip()
            fallback_provider = value("LLM_FALLBACK_PROVIDER", "").strip().casefold()
            if fallback_provider not in {"", "groq", "gemini"}:
                raise ValueError("LLM_FALLBACK_PROVIDER must be groq or gemini.")
            fallback = provider_settings.get(fallback_provider)
        elif primary_provider in provider_settings:
            base_url, api_key, model = provider_settings[primary_provider]
            fallback_provider = "gemini" if primary_provider == "groq" else "groq"
            fallback = provider_settings[fallback_provider]
        else:
            raise ValueError("LLM_PRIMARY_PROVIDER must be groq, gemini, or custom.")
        base_url = base_url.strip().rstrip("/")
        if fallback:
            fallback_base_url, fallback_api_key, fallback_model = fallback
            fallback_base_url = fallback_base_url.strip().rstrip("/")
        else:
            fallback_base_url, fallback_api_key, fallback_model = "", "", ""

        try:
            max_retries = int(value("LLM_MAX_RETRIES", "1"))
            timeout_seconds = float(value("LLM_TIMEOUT_SECONDS", "45"))
            retry_backoff_seconds = float(value("LLM_RETRY_BACKOFF_SECONDS", "0.5"))
        except ValueError as error:
            raise ValueError(
                "LLM_MAX_RETRIES, LLM_TIMEOUT_SECONDS, and "
                "LLM_RETRY_BACKOFF_SECONDS must be numeric."
            ) from error
        if not 0 <= max_retries <= 5:
            raise ValueError("LLM_MAX_RETRIES must be between 0 and 5.")
        if not 0 < timeout_seconds <= 300:
            raise ValueError("LLM_TIMEOUT_SECONDS must be greater than 0 and at most 300.")
        if not 0 <= retry_backoff_seconds <= 30:
            raise ValueError(
                "LLM_RETRY_BACKOFF_SECONDS must be between 0 and 30."
            )
        database_path = value("SMART_HOME_DB_PATH", "data/smart_home.sqlite3").strip()
        timezone_name = value("SMART_HOME_TIMEZONE", "Asia/Kolkata").strip()
        if not base_url or not model or not database_path:
            raise ValueError(
                "The selected LLM provider URL and model, and "
                "SMART_HOME_DB_PATH, cannot be empty."
            )
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as error:
            raise ValueError(
                f"SMART_HOME_TIMEZONE must be a valid IANA timezone; got {timezone_name!r}."
            ) from error
        return cls(
            llm_base_url=base_url,
            llm_api_key=api_key,
            llm_model=model,
            database_path=database_path,
            timezone=timezone,
            llm_primary_provider=primary_provider,
            llm_fallback_provider=fallback_provider,
            llm_fallback_base_url=fallback_base_url,
            llm_fallback_api_key=fallback_api_key,
            llm_fallback_model=fallback_model,
            llm_max_retries=max_retries,
            llm_timeout_seconds=timeout_seconds,
            llm_retry_backoff_seconds=retry_backoff_seconds,
        )
